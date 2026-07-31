from __future__ import annotations

import asyncio
import html
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .feed_client import FeedItem, fetch_user_feed, normalize_uid
from .html_card_renderer import render_weibo_card_html
from .post_client import fetch_weibo_post, is_weibo_url
from .storage import Subscription, WeiboFeedStore


_URL_RE = re.compile(r"https?://[^\s<>'\"`，。！？、；：）)\]}]+", re.I)
_WEIBO_COMMANDS = (
    "微博帮助",
    "微博订阅",
    "微博取消订阅",
    "微博订阅列表",
    "微博推送测试",
    "微博推送开",
    "微博推送关",
)


def _split_ids(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return {part.strip() for part in str(value).replace("\n", ",").split(",") if part.strip()}


def _clamp(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n...[已截断]"


def _event_group_id(event: AstrMessageEvent) -> str:
    return str(event.get_group_id() or "").strip()


def _event_origin(event: AstrMessageEvent) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "")


def _origin_is_group(origin: str) -> bool:
    value = str(origin or "").lower()
    return value.startswith("group:") or ":groupmessage:" in value


def _target_kind(event: AstrMessageEvent) -> str:
    return "group" if _event_group_id(event) else "private"


def _message_components(event: AstrMessageEvent) -> list[object]:
    message_obj = getattr(event, "message_obj", None)
    message = getattr(message_obj, "message", None)
    if isinstance(message, list):
        return message
    if message is not None:
        try:
            return list(message)
        except TypeError:
            pass
    return []


def _json_payloads(event: AstrMessageEvent) -> list[object]:
    payloads: list[object] = []
    for component in _message_components(event):
        type_value = getattr(component, "type", "")
        type_name = str(getattr(type_value, "value", type_value) or component.__class__.__name__).lower()
        if type_name != "json" and component.__class__.__name__.lower() != "json":
            continue
        data = getattr(component, "data", None)
        if data is not None:
            payloads.append(data)
    return payloads


def _iter_text_values(value: object):
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_text_values(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_text_values(child)
        return
    if not isinstance(value, str):
        return

    yield value
    nested = value.strip()
    if nested.startswith(("{", "[")):
        try:
            yield from _iter_text_values(json.loads(nested))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass


def _normalize_url(value: str) -> str:
    url = html.unescape(str(value or "")).replace("\\/", "/").strip()
    for _ in range(2):
        decoded = unquote(url)
        if decoded == url:
            break
        url = decoded
    return url.rstrip(".,;:!?，。！？、；：)]}）")


def _extract_weibo_links(event: AstrMessageEvent) -> list[str]:
    values: list[object] = _json_payloads(event)
    if event.message_str:
        values.append(event.message_str)

    links: list[str] = []
    seen: set[str] = set()
    for value in values:
        for text in _iter_text_values(value):
            normalized_text = html.unescape(text).replace("\\/", "/")
            for match in _URL_RE.finditer(normalized_text):
                url = _normalize_url(match.group(0))
                key = url.lower()
                if is_weibo_url(url) and key not in seen:
                    seen.add(key)
                    links.append(url)
    return links


def _is_weibo_command(text: str) -> bool:
    value = str(text or "").strip()
    if value.startswith("/"):
        value = value[1:].lstrip()
    command = value.split(maxsplit=1)[0] if value else ""
    return command in _WEIBO_COMMANDS


def _command_argument(text: str, commands: tuple[str, ...]) -> str:
    first_line = str(text or "").strip().splitlines()[0].strip() if str(text or "").strip() else ""
    if first_line.startswith("/"):
        first_line = first_line[1:].lstrip()
    for command in commands:
        if first_line == command:
            return ""
        if first_line.startswith(command + " "):
            return first_line[len(command) :].strip()
        if first_line.startswith(command):
            value = first_line[len(command) :].strip()
            if value:
                return value
    return ""


def _help_text() -> str:
    return "\n".join(
        [
            "微博 UID 更新推送",
            "",
            "订阅当前会话：/微博订阅 7429448199",
            "也支持链接：/微博订阅 https://weibo.com/u/7429448199",
            "取消当前会话：/微博取消订阅 7429448199",
            "查看当前会话：/微博订阅列表",
            "测试抓取：/微博推送测试 7429448199",
            "暂停当前会话：/微博推送关",
            "恢复当前会话：/微博推送开",
            "",
            "说明：低频轮询，非实时推送；需要先部署并配置可访问的 weibo-rss 服务。",
        ]
    )


@register(
    "astrbot_plugin_weibo_feed",
    "NightingaleSilence",
    "微博 UID 低频轮询推送插件。",
    "0.1.0",
)
class WeiboFeedPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(__file__).resolve().parent / ".local"
        self.store = WeiboFeedStore(self.data_dir)
        self.image_dir = self.data_dir / "images"
        self.card_dir = self.data_dir / "cards"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.card_dir.mkdir(parents=True, exist_ok=True)
        self.max_output_chars = int(self.config.get("max_output_chars", 1800) or 1800)
        self._poll_task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.config.get("enabled", True):
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("Weibo feed poll loop started.")

    async def terminate(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    def _base_url(self) -> str:
        return str(self.config.get("weibo_rss_base_url", "http://docker-weibo-rss:3000") or "http://docker-weibo-rss:3000").rstrip("/")

    def _resolve_shared_posts(self) -> bool:
        return bool(self.config.get("resolve_shared_posts", True))

    def _post_fetch_timeout_seconds(self) -> int:
        return max(5, int(self.config.get("post_fetch_timeout_seconds", 15) or 15))

    def _post_api_proxy_url(self) -> str:
        return str(self.config.get("post_api_proxy_url", "") or "").strip()

    def _poll_interval_seconds(self) -> int:
        minutes = int(self.config.get("poll_interval_minutes", 10) or 10)
        return max(5, minutes) * 60

    def _max_items_per_poll(self) -> int:
        return max(1, int(self.config.get("max_items_per_poll", 3) or 3))

    def _initial_backfill_items(self) -> int:
        return max(0, int(self.config.get("initial_backfill_items", 1) or 0))

    def _include_images(self) -> bool:
        return bool(self.config.get("include_images", False))

    def _render_card_image(self) -> bool:
        return bool(self.config.get("render_card_image", True))

    def _card_width(self) -> int:
        return max(640, int(self.config.get("card_width", 860) or 860))

    def _card_max_height(self) -> int:
        return max(1200, int(self.config.get("card_max_height", 2800) or 2800))

    def _brand_name(self) -> str:
        return str(self.config.get("card_brand_name", "Yoine❤") or "Yoine❤").strip() or "Yoine❤"

    def _brand_avatar_url(self) -> str:
        return str(
            self.config.get(
                "card_brand_avatar_url",
                "https://q1.qlogo.cn/g?b=qq&nk=1756507015&s=640",
            )
            or ""
        ).strip()

    def _group_use_forward(self) -> bool:
        return bool(self.config.get("group_use_forward", True))

    def _forward_display_name(self) -> str:
        return str(self.config.get("forward_display_name", "Yoine♡") or "Yoine♡").strip() or "Yoine♡"

    def _forward_display_uin(self) -> str:
        return str(self.config.get("forward_display_uin", "1756507015") or "1756507015").strip() or "1756507015"

    def _max_images_per_post(self) -> int:
        return max(0, int(self.config.get("max_images_per_post", 3) or 0))

    def _image_proxy_url(self) -> str:
        return str(self.config.get("image_proxy_url", "") or "").strip()

    def _image_download_timeout_seconds(self) -> int:
        return max(3, int(self.config.get("image_download_timeout_seconds", 20) or 20))

    def _max_image_bytes(self) -> int:
        return max(100_000, int(self.config.get("max_image_bytes", 8_000_000) or 8_000_000))

    def _failure_notice_threshold(self) -> int:
        return max(1, int(self.config.get("failure_notice_threshold", 6) or 6))

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admin_ids = _split_ids(self.config.get("admin_user_ids", ""))
        return not admin_ids or str(event.get_sender_id()) in admin_ids

    async def _fetch_feed(self, uid: str) -> list[FeedItem]:
        return await asyncio.to_thread(fetch_user_feed, self._base_url(), uid)

    async def _fetch_shared_post(self, url: str) -> tuple[str, FeedItem]:
        return await asyncio.to_thread(
            fetch_weibo_post,
            url,
            timeout=self._post_fetch_timeout_seconds(),
            proxy_url=self._post_api_proxy_url(),
        )

    async def _send_to_origin(self, origin: str, text: str) -> None:
        if not origin:
            return
        await self.context.send_message(origin, MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]))

    async def _send_item_to_origin(self, origin: str, uid: str, item: FeedItem) -> None:
        card_path = await self._render_item_card_path(item) if self._render_card_image() else ""
        if _origin_is_group(origin) and self._group_use_forward():
            node = await self._build_forward_node(uid, item, card_path=card_path)
            if node is not None:
                await self.context.send_message(origin, MessageChain([Comp.Nodes([node])]))
                return
        if card_path:
            await self.context.send_message(origin, MessageChain([Comp.Image.fromFileSystem(card_path)]))
            if item.link:
                await self.context.send_message(origin, MessageChain([Comp.Plain(item.link)]))
            await self._send_item_images_to_origin(origin, item)
            return
        await self._send_to_origin(origin, self._format_item(uid, item))
        await self._send_item_images_to_origin(origin, item)

    async def _send_item_images_to_origin(self, origin: str, item: FeedItem) -> None:
        if not origin or not self._include_images():
            return
        for image_url in item.image_urls[: self._max_images_per_post()]:
            try:
                image_path = await self._download_image(image_url)
                await self.context.send_message(
                    origin,
                    MessageChain([Comp.Image.fromFileSystem(image_path)]),
                )
            except Exception as error:
                logger.warning("Weibo feed image send failed for %s: %s", item.link, error)

    async def _build_forward_node(
        self,
        uid: str,
        item: FeedItem,
        *,
        card_path: str = "",
    ) -> Comp.Node | None:
        content: list[Comp.BaseMessageComponent] = []
        if card_path:
            content.append(Comp.Image.fromFileSystem(card_path))
            if item.link:
                content.append(Comp.Plain(item.link))
        else:
            content.append(Comp.Plain(self._format_item(uid, item)))
        if self._include_images():
            for image_url in item.image_urls[: self._max_images_per_post()]:
                try:
                    image_path = await self._download_image(image_url)
                    content.append(Comp.Image.fromFileSystem(image_path))
                except Exception as error:
                    logger.warning("Weibo feed forward image build failed for %s: %s", item.link, error)
        if not content:
            return None
        return Comp.Node(
            content=content,
            name=self._forward_display_name(),
            uin=self._forward_display_uin(),
        )

    async def _send_item_to_event(self, event: AstrMessageEvent, uid: str, item: FeedItem, prefix: str = "") -> None:
        card_path = await self._render_item_card_path(item) if self._render_card_image() else ""
        group_id = _event_group_id(event)
        if group_id and self._group_use_forward():
            node = await self._build_forward_node(uid, item, card_path=card_path)
            if node is not None:
                if prefix:
                    await event.send(MessageChain([Comp.Plain(_clamp(prefix, self.max_output_chars))]))
                await event.send(MessageChain([Comp.Nodes([node])]))
                return
        if card_path:
            if prefix:
                await event.send(MessageChain([Comp.Plain(_clamp(prefix, self.max_output_chars))]))
            await event.send(MessageChain([Comp.Image.fromFileSystem(card_path)]))
            if item.link:
                await event.send(MessageChain([Comp.Plain(item.link)]))
            await self._send_item_images_to_event(event, item)
            return
        text = (prefix + "\n" if prefix else "") + self._format_item(uid, item)
        if self._include_images() and item.image_urls:
            await event.send(MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]))
            await self._send_item_images_to_event(event, item)
            return
        await event.send(MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]))

    async def _send_item_images_to_event(self, event: AstrMessageEvent, item: FeedItem) -> None:
        if not self._include_images():
            return
        for image_url in item.image_urls[: self._max_images_per_post()]:
            try:
                image_path = await self._download_image(image_url)
                await event.send(MessageChain([Comp.Image.fromFileSystem(image_path)]))
            except Exception as error:
                logger.warning("Weibo feed command image send failed for %s: %s", item.link, error)

    async def _download_image(self, image_url: str) -> str:
        return await asyncio.to_thread(self._download_image_sync, image_url)

    async def _render_item_card_path(self, item: FeedItem) -> str:
        if not self._render_card_image():
            return ""
        image_paths: list[str] = []
        for image_url in item.image_urls[: self._max_images_per_post()]:
            try:
                image_paths.append(await self._download_image(image_url))
            except Exception as error:
                logger.warning("Weibo feed card image download failed for %s: %s", item.link, error)

        author_avatar_path = ""
        if item.author_avatar_url:
            try:
                author_avatar_path = await self._download_image(item.author_avatar_url)
            except Exception as error:
                logger.warning("Weibo feed author avatar download failed for %s: %s", item.link, error)

        brand_avatar_path = ""
        if self._brand_avatar_url():
            try:
                brand_avatar_path = await self._download_image(self._brand_avatar_url())
            except Exception as error:
                logger.warning("Weibo feed card brand avatar download failed: %s", error)
        try:
            return await render_weibo_card_html(
                self,
                item,
                self.card_dir,
                image_paths=image_paths,
                author_avatar_path=author_avatar_path,
                brand_avatar_path=brand_avatar_path,
                brand_name=self._brand_name(),
                width=self._card_width(),
                max_height=self._card_max_height(),
            )
        except Exception as error:
            logger.warning("Weibo feed HTML card render failed for %s: %s", item.link, error)
            return ""

    def _download_image_sync(self, image_url: str) -> str:
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("图片 URL 协议不支持。")

        extension = self._image_extension(parsed.path)
        digest = sha256(image_url.encode("utf-8")).hexdigest()[:24]
        target = self.image_dir / f"{digest}{extension}"
        if target.exists() and 0 < target.stat().st_size <= self._max_image_bytes():
            return str(target)

        handlers = []
        proxy_url = self._image_proxy_url()
        if proxy_url:
            handlers.append(ProxyHandler({"http": proxy_url, "https": proxy_url}))
        opener = build_opener(*handlers)
        request = Request(
            image_url,
            headers={
                "User-Agent": "Mozilla/5.0 NightingaleOpsBot-WeiboFeed/0.1",
                "Referer": "https://weibo.com/",
            },
        )
        try:
            with opener.open(request, timeout=self._image_download_timeout_seconds()) as response:
                content_type = response.headers.get("Content-Type", "")
                if not content_type.lower().startswith("image/"):
                    raise RuntimeError(f"图片响应类型不正确：{content_type}")
                data = response.read(self._max_image_bytes() + 1)
        except URLError as error:
            raise RuntimeError(f"下载图片失败：{error.reason}") from error

        if len(data) > self._max_image_bytes():
            raise RuntimeError("图片过大，已跳过。")
        if not data:
            raise RuntimeError("图片为空，已跳过。")
        target.write_bytes(data)
        return str(target)

    @staticmethod
    def _image_extension(path: str) -> str:
        suffix = Path(path).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            return ".jpg" if suffix == ".jpeg" else suffix
        return ".jpg"

    async def _poll_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Weibo feed poll loop error: %s", error)
            await asyncio.sleep(self._poll_interval_seconds())

    async def _poll_once(self) -> None:
        async with self._poll_lock:
            subscriptions = self.store.list_enabled()
            for subscription in subscriptions:
                await self._process_subscription(subscription)

    async def _process_subscription(self, subscription: Subscription) -> None:
        try:
            items = await self._fetch_feed(subscription.uid)
        except Exception as error:
            failure_count = self.store.update_failure(subscription.id, str(error))
            if failure_count >= self._failure_notice_threshold():
                logger.warning(
                    "Weibo feed failed for %s to %s for %s times: %s",
                    subscription.uid,
                    subscription.target_kind,
                    failure_count,
                    error,
                )
            else:
                logger.info("Weibo feed failed for %s: %s", subscription.uid, error)
            return

        if not items:
            return

        new_items = self._new_items(subscription, items)
        if not new_items:
            self.store.update_success(
                subscription.id,
                last_seen_id=items[0].item_id,
                last_seen_link=items[0].link,
            )
            return

        for item in reversed(new_items[: self._max_items_per_poll()]):
            await self._send_item_to_origin(subscription.target_origin, subscription.uid, item)
            self.store.record_seen(
                uid=subscription.uid,
                item_id=item.item_id,
                link=item.link,
                published_at=item.published_at,
            )

        self.store.update_success(
            subscription.id,
            last_seen_id=items[0].item_id,
            last_seen_link=items[0].link,
        )

    @staticmethod
    def _new_items(subscription: Subscription, items: list[FeedItem]) -> list[FeedItem]:
        if not subscription.last_seen_id and not subscription.last_seen_link:
            return []
        result: list[FeedItem] = []
        for item in items:
            if item.item_id == subscription.last_seen_id or (
                item.link and item.link == subscription.last_seen_link
            ):
                break
            result.append(item)
        return result

    def _format_item(self, uid: str, item: FeedItem) -> str:
        label = item.author_name or f"UID {uid}"
        body = str(item.summary or item.title or "").strip()
        lines = [f"微博更新：{label}"]
        if body:
            lines.append(body)
        if item.published_at:
            lines.append(item.published_at)
        if item.link:
            lines.append(item.link)
        return _clamp("\n".join(lines), self.max_output_chars)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def resolve_weibo_share(self, event: AstrMessageEvent):
        if not self._resolve_shared_posts() or _is_weibo_command(event.message_str or ""):
            return

        links = _extract_weibo_links(event)
        if not links:
            return

        event.stop_event()
        try:
            post_id, item = await self._fetch_shared_post(links[0])
            await self._send_item_to_event(event, post_id, item)
        except Exception as error:
            logger.info("Weibo shared post resolve failed for %s: %s", links[0], error)

    @filter.command("微博帮助")
    async def weibo_help(self, event: AstrMessageEvent):
        yield event.plain_result(_help_text())

    @filter.command("微博订阅")
    async def subscribe_weibo(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("你没有管理微博推送订阅的权限。")
            return

        origin = _event_origin(event)
        if not origin:
            yield event.plain_result("当前会话来源无法记录，请稍后重试。")
            return

        try:
            uid = normalize_uid(_command_argument(event.message_str or "", ("微博订阅",)))
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        try:
            items = await self._fetch_feed(uid)
        except Exception as error:
            yield event.plain_result(_clamp(f"订阅前测试失败：{error}", self.max_output_chars))
            return

        latest = items[0] if items else None
        subscription, created = self.store.upsert_subscription(
            uid=uid,
            target_origin=origin,
            target_kind=_target_kind(event),
            created_by=str(event.get_sender_id()),
            last_seen_id=latest.item_id if latest else "",
            last_seen_link=latest.link if latest else "",
        )

        lines = [f"已订阅微博 UID {subscription.uid}。" if created else f"微博 UID {subscription.uid} 已恢复订阅。"]
        backfill = self._initial_backfill_items()
        if latest and backfill > 0:
            lines.append("")
            lines.append("当前最新：")
            for item in items[:backfill]:
                lines.append(self._format_item(subscription.uid, item))
                self.store.record_seen(
                    uid=subscription.uid,
                    item_id=item.item_id,
                    link=item.link,
                    published_at=item.published_at,
                )
        text = _clamp("\n\n".join(lines), self.max_output_chars)
        if latest and (
            self._render_card_image()
            or (_event_group_id(event) and self._group_use_forward())
            or (self._include_images() and latest.image_urls)
        ):
            await event.send(MessageChain([Comp.Plain(text)]))
            await self._send_item_to_event(event, subscription.uid, latest)
            return
        yield event.plain_result(text)

    @filter.command("微博取消订阅")
    async def unsubscribe_weibo(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("你没有管理微博推送订阅的权限。")
            return
        origin = _event_origin(event)
        try:
            uid = normalize_uid(_command_argument(event.message_str or "", ("微博取消订阅",)))
        except ValueError as error:
            yield event.plain_result(str(error))
            return
        removed = self.store.remove_subscription(uid=uid, target_origin=origin)
        yield event.plain_result(f"已取消订阅微博 UID {uid}。" if removed else f"当前会话没有订阅微博 UID {uid}。")

    @filter.command("微博订阅列表")
    async def list_weibo_subscriptions(self, event: AstrMessageEvent):
        origin = _event_origin(event)
        subscriptions = self.store.list_for_origin(origin)
        if not subscriptions:
            yield event.plain_result("当前会话还没有微博订阅。")
            return
        lines = ["当前会话的微博订阅："]
        for subscription in subscriptions:
            status = "开启" if subscription.enabled else "暂停"
            last = subscription.last_success_at or "-"
            fail = f"，连续失败 {subscription.failure_count} 次" if subscription.failure_count else ""
            lines.append(f"- {subscription.uid}：{status}，上次成功 {last}{fail}")
        yield event.plain_result(_clamp("\n".join(lines), self.max_output_chars))

    @filter.command("微博推送测试")
    async def test_weibo_feed(self, event: AstrMessageEvent):
        try:
            uid = normalize_uid(_command_argument(event.message_str or "", ("微博推送测试",)))
        except ValueError as error:
            yield event.plain_result(str(error))
            return
        try:
            items = await self._fetch_feed(uid)
        except Exception as error:
            yield event.plain_result(_clamp(f"测试失败：{error}", self.max_output_chars))
            return
        if not items:
            yield event.plain_result(f"微博 UID {uid} 当前没有可推送条目。")
            return
        await self._send_item_to_event(event, uid, items[0], prefix="测试成功：")

    @filter.command("微博推送开")
    async def enable_weibo_feed(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("你没有管理微博推送订阅的权限。")
            return
        count = self.store.set_origin_enabled(_event_origin(event), True)
        yield event.plain_result(f"已开启当前会话的微博推送订阅：{count} 个。")

    @filter.command("微博推送关")
    async def disable_weibo_feed(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("你没有管理微博推送订阅的权限。")
            return
        count = self.store.set_origin_enabled(_event_origin(event), False)
        yield event.plain_result(f"已暂停当前会话的微博推送订阅：{count} 个。")
