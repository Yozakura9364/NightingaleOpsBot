from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
import re
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .card_renderer import render_bili_card
from .feed_client import (
    FeedItem,
    extract_cookie_value,
    fetch_cookie_dynamic_detail,
    fetch_cookie_user_feed,
    fetch_user_feed,
    normalize_cookie,
    normalize_uid,
)
from .html_card_renderer import render_bili_card_html
from .storage import BiliFeedStore, Subscription


SUBSCRIBE_COMMANDS = ("bili_sub", "订阅动态")
LIST_COMMANDS = ("bili_sub_list", "订阅列表")
DELETE_COMMANDS = ("bili_sub_del", "订阅删除")
TEST_COMMANDS = ("bili_sub_test", "订阅测试")
ENABLE_COMMANDS = ("bili_sub_on", "订阅开启")
DISABLE_COMMANDS = ("bili_sub_off", "订阅关闭")
HELP_COMMANDS = ("bili_help", "B站帮助")
BIND_COMMANDS = ("bili_bind", "B站绑定")
STATUS_COMMANDS = ("bili_status", "B站状态")
UNBIND_COMMANDS = ("bili_unbind", "B站解绑")

_DEFAULT_API_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
_GENERIC_DYNAMIC_SUMMARIES = {
    "发布了动态",
    "发布了图片动态",
    "发布了图文动态",
    "发布了视频动态",
    "发布了专栏动态",
    "转发了一条动态",
}


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


def _target_kind(event: AstrMessageEvent) -> str:
    return "group" if _event_group_id(event) else "private"


def _is_private(event: AstrMessageEvent) -> bool:
    return not _event_group_id(event)


def _private_origin(event: AstrMessageEvent) -> str:
    return _event_origin(event) if _is_private(event) else ""


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


def _first_line(text: str) -> str:
    value = str(text or "").strip()
    return value.splitlines()[0].strip() if value else ""


def _strip_leading_slash(text: str) -> str:
    value = _first_line(text)
    if value.startswith("/"):
        return value[1:].lstrip()
    return value


def _parse_subscribe_args(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("请提供 B 站 UID。")
    parts = raw.split(maxsplit=1)
    uid = normalize_uid(parts[0])
    keyword_filter = parts[1].strip() if len(parts) > 1 else ""
    return uid, keyword_filter


def _split_filter_terms(value: str) -> list[str]:
    return [term.strip().lower() for term in str(value or "").replace("\n", ",").split(",") if term.strip()]


def _matches_filter(item: FeedItem, keyword_filter: str) -> bool:
    terms = _split_filter_terms(keyword_filter)
    if not terms:
        return True
    haystack = "\n".join(
        [
            str(item.title or ""),
            str(item.summary or ""),
            str(item.link or ""),
        ]
    ).lower()
    return any(term in haystack for term in terms)


def _extract_field(text: str, names: tuple[str, ...]) -> str:
    pattern = r"(?im)^\s*(?:" + "|".join(re.escape(name) for name in names) + r")\s*[:：]\s*(.+?)\s*$"
    match = re.search(pattern, str(text or ""))
    return match.group(1).strip() if match else ""


def _has_bind_fields(text: str) -> bool:
    return bool(
        _extract_field(text, ("COOKIE", "Cookie", "cookie"))
        or _extract_field(text, ("SESSDATA",))
        or _extract_field(text, ("bili_jct", "BILI_JCT"))
        or _extract_field(text, ("DedeUserID", "DEDEUSERID"))
    )


def _cookie_preview(cookie: str) -> str:
    parts = []
    for key in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4"):
        value = extract_cookie_value(cookie, key)
        if not value:
            continue
        parts.append(f"{key}={_mask_value(value)}")
    return "；".join(parts)


def _mask_value(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) <= 8:
        return raw[:2] + "***" if raw else ""
    return raw[:4] + "..." + raw[-4:]


def _build_cookie_string(text: str) -> str:
    cookie_line = _extract_field(text, ("COOKIE", "Cookie", "cookie"))
    if cookie_line:
        return normalize_cookie(cookie_line)

    parts = []
    for key, names in (
        ("SESSDATA", ("SESSDATA",)),
        ("bili_jct", ("bili_jct", "BILI_JCT")),
        ("DedeUserID", ("DedeUserID", "DEDEUSERID")),
        ("buvid3", ("buvid3", "BUVID3")),
        ("buvid4", ("buvid4", "BUVID4")),
        ("ac_time_value", ("ac_time_value", "AC_TIME_VALUE")),
    ):
        value = _extract_field(text, names)
        if value:
            parts.append(f"{key}={value}")
    return normalize_cookie("; ".join(parts))


def _help_text() -> str:
    return "\n".join(
        [
            "B站动态订阅",
            "",
            "订阅命令：",
            "/bili_sub UID [关键词]",
            "/订阅动态 UID [关键词]",
            "/bili_sub_list",
            "/bili_sub_del UID",
            "/bili_sub_test UID",
            "/bili_sub_off",
            "/bili_sub_on",
            "",
            "登录态绑定：",
            "/bili_bind",
            "/B站绑定",
            "/bili_status",
            "/bili_unbind",
            "",
            "关键词可选，使用英文逗号分隔，命中任一关键词才推送。",
            "示例：/bili_sub 161775300 ff14,联动",
            "",
            "当前插件优先使用已绑定的 B 站 Cookie 直连动态接口；未绑定时才回退 RSSHub。",
            "Cookie 为全局共用登录态，请只在私聊里绑定。",
        ]
    )


def _bind_help_text() -> str:
    return "\n".join(
        [
            "B站 Cookie 绑定",
            "",
            "请私聊发送：",
            "/bili_bind",
            "SESSDATA: ...",
            "bili_jct: ...",
            "DedeUserID: ...",
            "buvid3: ...",
            "buvid4: ...",
            "USER_AGENT: Mozilla/5.0 ...",
            "",
            "也可以直接发完整 Cookie：",
            "/bili_bind",
            "COOKIE: SESSDATA=...; bili_jct=...; DedeUserID=...",
            "",
            "其中 SESSDATA、bili_jct、DedeUserID 必填。",
            "USER_AGENT 不填也可以，插件会使用默认浏览器 UA。",
        ]
    )


@register(
    "astrbot_plugin_bili_feed",
    "NightingaleSilence",
    "Bilibili UID low-frequency dynamic feed watcher.",
    "0.2.0",
)
class BiliFeedPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(__file__).resolve().parent / ".local"
        self.store = BiliFeedStore(self.data_dir)
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
            logger.info("Bili feed poll loop started.")

    async def terminate(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    def _rsshub_base_url(self) -> str:
        return str(self.config.get("rsshub_base_url", "http://rsshub:1200") or "http://rsshub:1200").rstrip("/")

    def _request_proxy_url(self) -> str:
        return str(self.config.get("request_proxy_url", "") or "").strip()

    def _request_timeout_seconds(self) -> int:
        return max(5, int(self.config.get("request_timeout_seconds", 30) or 30))

    def _poll_interval_seconds(self) -> int:
        minutes = int(self.config.get("poll_interval_minutes", 10) or 10)
        return max(5, minutes) * 60

    def _max_items_per_poll(self) -> int:
        return max(1, int(self.config.get("max_items_per_poll", 3) or 3))

    def _initial_backfill_items(self) -> int:
        return max(0, int(self.config.get("initial_backfill_items", 1) or 0))

    def _include_images(self) -> bool:
        return bool(self.config.get("include_images", True))

    def _max_images_per_post(self) -> int:
        return max(0, int(self.config.get("max_images_per_post", 1) or 0))

    def _image_proxy_url(self) -> str:
        return str(self.config.get("image_proxy_url", "") or "").strip()

    def _image_download_timeout_seconds(self) -> int:
        return max(3, int(self.config.get("image_download_timeout_seconds", 20) or 20))

    def _max_image_bytes(self) -> int:
        return max(100_000, int(self.config.get("max_image_bytes", 12_000_000) or 12_000_000))

    def _failure_notice_threshold(self) -> int:
        return max(1, int(self.config.get("failure_notice_threshold", 6) or 6))

    def _render_card_image(self) -> bool:
        return bool(self.config.get("render_card_image", True))

    def _card_width(self) -> int:
        return max(640, int(self.config.get("card_width", 860) or 860))

    def _card_max_height(self) -> int:
        return max(1200, int(self.config.get("card_max_height", 2800) or 2800))

    def _brand_name(self) -> str:
        return str(self.config.get("card_brand_name", "Yoine❤") or "Yoine❤").strip()

    def _brand_avatar_url(self) -> str:
        return str(
            self.config.get(
                "card_brand_avatar_url",
                "https://q1.qlogo.cn/g?b=qq&nk=1756507015&s=640",
            )
            or ""
        ).strip()

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admin_ids = _split_ids(self.config.get("admin_user_ids", ""))
        return not admin_ids or str(event.get_sender_id()) in admin_ids

    def _backend_label(self) -> str:
        return "直连 B站 API (Cookie)" if self.store.get_credential() else "RSSHub"

    async def _fetch_feed(self, uid: str) -> list[FeedItem]:
        credential = self.store.get_credential()
        if credential:
            return await asyncio.to_thread(
                fetch_cookie_user_feed,
                uid,
                cookie=credential.cookie,
                user_agent=credential.user_agent,
                timeout=float(self._request_timeout_seconds()),
                proxy_url=self._request_proxy_url(),
            )
        return await asyncio.to_thread(
            fetch_user_feed,
            self._rsshub_base_url(),
            uid,
            timeout=float(self._request_timeout_seconds()),
        )

    async def _reply_plain(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]))

    async def _send_to_origin(self, origin: str, text: str) -> None:
        if not origin:
            return
        await self.context.send_message(origin, MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]))

    async def _build_item_chain(self, text: str, item: FeedItem, log_prefix: str) -> MessageChain:
        content: list[Comp.BaseMessageComponent] = [Comp.Plain(_clamp(text, self.max_output_chars))]
        if self._include_images():
            for image_url in item.image_urls[: self._max_images_per_post()]:
                try:
                    image_path = await self._download_image(image_url)
                    content.append(Comp.Image.fromFileSystem(image_path))
                except Exception as error:
                    logger.warning("%s image build failed for %s: %s", log_prefix, item.link, error)
        return MessageChain(content)

    async def _enrich_item_for_display(self, item: FeedItem) -> FeedItem:
        credential = self.store.get_credential()
        if not credential:
            return item
        if str(item.summary or "").strip() not in _GENERIC_DYNAMIC_SUMMARIES:
            return item
        try:
            detail = await asyncio.to_thread(
                fetch_cookie_dynamic_detail,
                item.item_id,
                cookie=credential.cookie,
                user_agent=credential.user_agent,
                timeout=float(self._request_timeout_seconds()),
                proxy_url=self._request_proxy_url(),
            )
        except Exception as error:
            logger.warning("Bili feed detail fetch failed for %s: %s", item.link, error)
            return item
        if detail is None:
            return item
        return FeedItem(
            item_id=item.item_id,
            title=detail.title or item.title,
            link=detail.link or item.link,
            published_at=detail.published_at or item.published_at,
            summary=detail.summary or item.summary,
            author_name=detail.author_name or item.author_name,
            image_urls=detail.image_urls or item.image_urls,
            avatar_url=detail.avatar_url or item.avatar_url,
        )

    async def _render_item_card_path(self, item: FeedItem) -> str:
        if not self._render_card_image():
            return ""
        image_path = ""
        avatar_path = ""
        brand_avatar_path = ""
        if item.image_urls:
            try:
                image_path = await self._download_image(item.image_urls[0])
            except Exception as error:
                logger.warning("Bili feed card image download failed for %s: %s", item.link, error)
        if item.avatar_url:
            try:
                avatar_path = await self._download_image(item.avatar_url)
            except Exception as error:
                logger.warning("Bili feed author avatar download failed for %s: %s", item.link, error)
        if self._brand_avatar_url():
            try:
                brand_avatar_path = await self._download_image(self._brand_avatar_url())
            except Exception as error:
                logger.warning("Bili feed brand avatar download failed: %s", error)
        try:
            return await render_bili_card_html(
                self,
                item,
                self.card_dir,
                image_path=image_path,
                avatar_path=avatar_path,
                brand_avatar_path=brand_avatar_path,
                brand_name=self._brand_name(),
                width=self._card_width(),
                max_height=self._card_max_height(),
            )
        except Exception as error:
            logger.warning("Bili feed HTML card render failed for %s: %s", item.link, error)
        try:
            return str(
                await asyncio.to_thread(
                    render_bili_card,
                    item,
                    self.card_dir,
                    image_path=image_path,
                    width=self._card_width(),
                    max_height=self._card_max_height(),
                )
            )
        except Exception as error:
            logger.warning("Bili feed fallback card render failed for %s: %s", item.link, error)
            return ""

    async def _send_item_to_origin(self, origin: str, uid: str, item: FeedItem) -> None:
        if not origin:
            return
        card_path = await self._render_item_card_path(item)
        if card_path:
            await self.context.send_message(origin, MessageChain([Comp.Image.fromFileSystem(card_path)]))
            if item.link:
                await self.context.send_message(origin, MessageChain([Comp.Plain(item.link)]))
            return
        chain = await self._build_item_chain(self._format_item(uid, item), item, "Bili feed push")
        await self.context.send_message(origin, chain)

    async def _send_item_images_to_origin(self, origin: str, item: FeedItem) -> None:
        if not origin or not self._include_images():
            return
        for image_url in item.image_urls[: self._max_images_per_post()]:
            try:
                image_path = await self._download_image(image_url)
                await self.context.send_message(origin, MessageChain([Comp.Image.fromFileSystem(image_path)]))
            except Exception as error:
                logger.warning("Bili feed image send failed for %s: %s", item.link, error)

    async def _send_item_to_event(self, event: AstrMessageEvent, uid: str, item: FeedItem, prefix: str = "") -> None:
        card_path = await self._render_item_card_path(item)
        if card_path:
            if prefix:
                await event.send(MessageChain([Comp.Plain(_clamp(prefix, self.max_output_chars))]))
            await event.send(MessageChain([Comp.Image.fromFileSystem(card_path)]))
            if item.link:
                await event.send(MessageChain([Comp.Plain(item.link)]))
            return
        text = (prefix + "\n" if prefix else "") + self._format_item(uid, item)
        chain = await self._build_item_chain(text, item, "Bili feed command")
        await event.send(chain)

    async def _send_item_images_to_event(self, event: AstrMessageEvent, item: FeedItem) -> None:
        if not self._include_images():
            return
        for image_url in item.image_urls[: self._max_images_per_post()]:
            try:
                image_path = await self._download_image(image_url)
                await event.send(MessageChain([Comp.Image.fromFileSystem(image_path)]))
            except Exception as error:
                logger.warning("Bili feed command image send failed for %s: %s", item.link, error)

    async def _download_image(self, image_url: str) -> str:
        return await asyncio.to_thread(self._download_image_sync, image_url)

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
                "User-Agent": _DEFAULT_API_USER_AGENT,
                "Referer": "https://www.bilibili.com/",
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
                logger.error("Bili feed poll loop error: %s", error)
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
                    "Bili feed failed for %s to %s for %s times: %s",
                    subscription.uid,
                    subscription.target_kind,
                    failure_count,
                    error,
                )
            else:
                logger.info("Bili feed failed for %s: %s", subscription.uid, error)
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

        matched_items = [item for item in new_items if _matches_filter(item, subscription.keyword_filter)]
        for item in reversed(matched_items[: self._max_items_per_poll()]):
            item = await self._enrich_item_for_display(item)
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
        lines = [f"B站动态更新：{label}"]
        if body:
            lines.append(body)
        if item.published_at:
            lines.append(item.published_at)
        if item.link:
            lines.append(item.link)
        return _clamp("\n".join(lines), self.max_output_chars)

    def _format_subscription(self, subscription: Subscription) -> str:
        status = "开启" if subscription.enabled else "暂停"
        last = subscription.last_success_at or "-"
        fail = f"，连续失败 {subscription.failure_count} 次" if subscription.failure_count else ""
        filter_text = f"，关键词：{subscription.keyword_filter}" if subscription.keyword_filter else ""
        return f"- {subscription.uid}：{status}，上次成功 {last}{filter_text}{fail}"

    async def _handle_help(self, event: AstrMessageEvent) -> None:
        await self._reply_plain(event, _help_text())

    async def _handle_bind(self, event: AstrMessageEvent) -> None:
        if not self._is_admin(event):
            await self._reply_plain(event, "你没有管理 B站动态登录态的权限。")
            return
        if not _is_private(event):
            await self._reply_plain(event, "为了保护登录态，请私聊发送 /bili_bind。")
            return

        text = event.message_str or ""
        if not _has_bind_fields(text):
            await self._reply_plain(event, _bind_help_text())
            return

        origin = _private_origin(event)
        if not origin:
            await self._reply_plain(event, "当前私聊来源无法记录，请稍后重试。")
            return

        try:
            cookie = _build_cookie_string(text)
        except ValueError as error:
            await self._reply_plain(event, str(error))
            return

        user_agent = _extract_field(text, ("USER_AGENT", "User-Agent", "user_agent", "UA"))
        normalized_user_agent = str(user_agent or _DEFAULT_API_USER_AGENT).strip() or _DEFAULT_API_USER_AGENT
        dede_user_id = extract_cookie_value(cookie, "DedeUserID")
        display_name = _extract_field(text, ("DISPLAY_NAME", "display_name", "账号", "昵称"))
        display_name = str(display_name or "").strip() or (f"DedeUserID {dede_user_id}" if dede_user_id else "B站登录态")

        self.store.bind_credential(
            owner_user_id=str(event.get_sender_id()),
            private_origin=origin,
            display_name=display_name,
            cookie=cookie,
            user_agent=normalized_user_agent,
        )
        await self._reply_plain(
            event,
            "\n".join(
                [
                    "B站 Cookie 绑定成功。",
                    f"账号：{display_name}",
                    f"预览：{_cookie_preview(cookie)}",
                    f"抓取后端：{self._backend_label()}",
                ]
            ),
        )

    async def _handle_status(self, event: AstrMessageEvent) -> None:
        if not self._is_admin(event):
            await self._reply_plain(event, "你没有查看 B站动态登录态的权限。")
            return
        if not _is_private(event):
            await self._reply_plain(event, "为了保护登录态，请私聊发送 /bili_status。")
            return

        credential = self.store.get_credential()
        if not credential:
            await self._reply_plain(
                event,
                "\n".join(
                    [
                        "当前未绑定 B站登录态。",
                        f"当前抓取后端：{self._backend_label()}",
                        "",
                        "私聊发送 /bili_bind 查看绑定格式。",
                    ]
                ),
            )
            return

        await self._reply_plain(
            event,
            "\n".join(
                [
                    "B站登录态已绑定。",
                    f"账号：{credential.display_name}",
                    f"更新时间：{credential.updated_at}",
                    f"抓取后端：{self._backend_label()}",
                    f"Cookie 预览：{_cookie_preview(credential.cookie)}",
                ]
            ),
        )

    async def _handle_unbind(self, event: AstrMessageEvent) -> None:
        if not self._is_admin(event):
            await self._reply_plain(event, "你没有管理 B站动态登录态的权限。")
            return
        if not _is_private(event):
            await self._reply_plain(event, "为了保护登录态，请私聊发送 /bili_unbind。")
            return
        removed = self.store.clear_credential()
        await self._reply_plain(event, "已解绑 B站登录态。" if removed else "当前没有已绑定的 B站登录态。")

    async def _handle_subscribe(self, event: AstrMessageEvent) -> None:
        if not self._is_admin(event):
            await self._reply_plain(event, "你没有管理 B站动态订阅的权限。")
            return

        origin = _event_origin(event)
        if not origin:
            await self._reply_plain(event, "当前会话来源无法记录，请稍后重试。")
            return

        try:
            uid, keyword_filter = _parse_subscribe_args(_command_argument(event.message_str or "", SUBSCRIBE_COMMANDS))
        except ValueError as error:
            await self._reply_plain(event, str(error))
            return

        try:
            items = await self._fetch_feed(uid)
        except Exception as error:
            await self._reply_plain(event, f"订阅前测试失败：{error}")
            return

        latest = items[0] if items else None
        subscription, created = self.store.upsert_subscription(
            uid=uid,
            keyword_filter=keyword_filter,
            target_origin=origin,
            target_kind=_target_kind(event),
            created_by=str(event.get_sender_id()),
            last_seen_id=latest.item_id if latest else "",
            last_seen_link=latest.link if latest else "",
        )

        lines = [f"已订阅 B站 UID {subscription.uid}。" if created else f"B站 UID {subscription.uid} 已更新订阅设置。"]
        lines.append(f"抓取后端：{self._backend_label()}")
        if subscription.keyword_filter:
            lines.append(f"关键词过滤：{subscription.keyword_filter}")
        backfill = self._initial_backfill_items()
        preview_items = [item for item in items if _matches_filter(item, subscription.keyword_filter)]
        if preview_items and backfill > 0:
            lines.append("")
            lines.append("当前最新：")
            preview_items = [await self._enrich_item_for_display(item) for item in preview_items[:backfill]] + preview_items[backfill:]
            for item in preview_items[:backfill]:
                lines.append(self._format_item(subscription.uid, item))
                self.store.record_seen(
                    uid=subscription.uid,
                    item_id=item.item_id,
                    link=item.link,
                    published_at=item.published_at,
                )
        text = "\n\n".join(lines)
        if preview_items and (self._render_card_image() or (self._include_images() and preview_items[0].image_urls)):
            await self._reply_plain(event, text)
            await self._send_item_to_event(event, subscription.uid, preview_items[0])
            return
        await self._reply_plain(event, text)

    async def _handle_unsubscribe(self, event: AstrMessageEvent) -> None:
        if not self._is_admin(event):
            await self._reply_plain(event, "你没有管理 B站动态订阅的权限。")
            return
        origin = _event_origin(event)
        try:
            uid = normalize_uid(_command_argument(event.message_str or "", DELETE_COMMANDS))
        except ValueError as error:
            await self._reply_plain(event, str(error))
            return
        removed = self.store.remove_subscription(uid=uid, target_origin=origin)
        await self._reply_plain(
            event,
            f"已取消订阅 B站 UID {uid}。" if removed else f"当前会话没有订阅 B站 UID {uid}。",
        )

    async def _handle_list(self, event: AstrMessageEvent) -> None:
        origin = _event_origin(event)
        subscriptions = self.store.list_for_origin(origin)
        if not subscriptions:
            await self._reply_plain(event, "当前会话还没有 B站动态订阅。")
            return
        lines = [f"当前会话的 B站动态订阅（{self._backend_label()}）："]
        for subscription in subscriptions:
            lines.append(self._format_subscription(subscription))
        await self._reply_plain(event, "\n".join(lines))

    async def _handle_test(self, event: AstrMessageEvent) -> None:
        try:
            uid = normalize_uid(_command_argument(event.message_str or "", TEST_COMMANDS))
        except ValueError as error:
            await self._reply_plain(event, str(error))
            return
        try:
            items = await self._fetch_feed(uid)
        except Exception as error:
            await self._reply_plain(event, f"测试失败：{error}")
            return
        if not items:
            await self._reply_plain(event, f"B站 UID {uid} 当前没有可推送条目。")
            return
        item = await self._enrich_item_for_display(items[0])
        await self._send_item_to_event(event, uid, item, prefix=f"测试成功（{self._backend_label()}）：")

    async def _handle_enable(self, event: AstrMessageEvent) -> None:
        if not self._is_admin(event):
            await self._reply_plain(event, "你没有管理 B站动态订阅的权限。")
            return
        count = self.store.set_origin_enabled(_event_origin(event), True)
        await self._reply_plain(event, f"已开启当前会话的 B站动态订阅：{count} 个。")

    async def _handle_disable(self, event: AstrMessageEvent) -> None:
        if not self._is_admin(event):
            await self._reply_plain(event, "你没有管理 B站动态订阅的权限。")
            return
        count = self.store.set_origin_enabled(_event_origin(event), False)
        await self._reply_plain(event, f"已暂停当前会话的 B站动态订阅：{count} 个。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def bili_raw_dispatch(self, event: AstrMessageEvent):
        raw = _strip_leading_slash(event.message_str or "")
        if not raw:
            return
        command_word = raw.split(maxsplit=1)[0].strip().lower()
        if command_word == "bili_help":
            event.stop_event()
            await self._handle_help(event)
            return
        if command_word == "bili_bind":
            event.stop_event()
            await self._handle_bind(event)
            return
        if command_word == "bili_status":
            event.stop_event()
            await self._handle_status(event)
            return
        if command_word == "bili_unbind":
            event.stop_event()
            await self._handle_unbind(event)
            return
        if command_word == "bili_sub":
            event.stop_event()
            await self._handle_subscribe(event)
            return
        if command_word == "bili_sub_del":
            event.stop_event()
            await self._handle_unsubscribe(event)
            return
        if command_word == "bili_sub_list":
            event.stop_event()
            await self._handle_list(event)
            return
        if command_word == "bili_sub_test":
            event.stop_event()
            await self._handle_test(event)
            return
        if command_word == "bili_sub_on":
            event.stop_event()
            await self._handle_enable(event)
            return
        if command_word == "bili_sub_off":
            event.stop_event()
            await self._handle_disable(event)
            return

    @filter.command("bili_help")
    @filter.command("B站帮助")
    async def bili_help(self, event: AstrMessageEvent):
        await self._handle_help(event)

    @filter.command("bili_bind")
    @filter.command("B站绑定")
    async def bind_bili(self, event: AstrMessageEvent):
        await self._handle_bind(event)

    @filter.command("bili_status")
    @filter.command("B站状态")
    async def bili_status(self, event: AstrMessageEvent):
        await self._handle_status(event)

    @filter.command("bili_unbind")
    @filter.command("B站解绑")
    async def unbind_bili(self, event: AstrMessageEvent):
        await self._handle_unbind(event)

    @filter.command("bili_sub")
    @filter.command("订阅动态")
    async def subscribe_bili(self, event: AstrMessageEvent):
        await self._handle_subscribe(event)

    @filter.command("bili_sub_del")
    @filter.command("订阅删除")
    async def unsubscribe_bili(self, event: AstrMessageEvent):
        await self._handle_unsubscribe(event)

    @filter.command("bili_sub_list")
    @filter.command("订阅列表")
    async def list_bili_subscriptions(self, event: AstrMessageEvent):
        await self._handle_list(event)

    @filter.command("bili_sub_test")
    @filter.command("订阅测试")
    async def test_bili_feed(self, event: AstrMessageEvent):
        await self._handle_test(event)

    @filter.command("bili_sub_on")
    @filter.command("订阅开启")
    async def enable_bili_feed(self, event: AstrMessageEvent):
        await self._handle_enable(event)

    @filter.command("bili_sub_off")
    @filter.command("订阅关闭")
    async def disable_bili_feed(self, event: AstrMessageEvent):
        await self._handle_disable(event)
