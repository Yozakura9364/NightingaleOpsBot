from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .feed_client import FeedItem, fetch_rsshub_user_feed, normalize_handle
from .storage import Subscription, XFeedStore
from .twikit_client import TwikitFeedClient, TwikitSettings


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
            "X 账号更新推送",
            "",
            "订阅当前会话：/x订阅 @handle",
            "取消当前会话：/x取消订阅 @handle",
            "查看当前会话：/x订阅列表",
            "测试抓取：/x推送测试 @handle",
            "暂停当前会话：/x推送关",
            "恢复当前会话：/x推送开",
            "翻译状态：/x翻译状态",
            "",
            "说明：低频轮询，非实时推送；默认后端为 Twikit，会读取 cookies 文件抓取 X。",
            "翻译：可在插件配置中启用，使用 AstrBot LLM Provider。",
        ]
    )


@register(
    "astrbot_plugin_x_feed",
    "NightingaleSilence",
    "X 账号低频轮询推送插件。",
    "0.1.0",
)
class XFeedPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(__file__).resolve().parent / ".local"
        self.store = XFeedStore(self.data_dir)
        self.image_dir = self.data_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.max_output_chars = int(self.config.get("max_output_chars", 1800) or 1800)
        self._poll_task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()
        self._twikit_client: TwikitFeedClient | None = None

    async def initialize(self) -> None:
        if self.config.get("enabled", True):
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("X feed poll loop started.")

    async def terminate(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    def _backend(self) -> str:
        value = str(self.config.get("backend", "twikit") or "twikit").strip().lower()
        return "rsshub" if value == "rsshub" else "twikit"

    def _backend_label(self) -> str:
        return "RSSHub" if self._backend() == "rsshub" else "Twikit"

    def _rsshub_base_url(self) -> str:
        return str(self.config.get("rsshub_base_url", "http://rsshub:1200") or "http://rsshub:1200").rstrip("/")

    def _twikit_cookies_file(self) -> Path:
        raw = str(self.config.get("twikit_cookies_file", ".local/x_cookies.json") or "").strip()
        if not raw:
            raw = ".local/x_cookies.json"
        path = Path(raw)
        return path if path.is_absolute() else (Path(__file__).resolve().parent / path)

    def _twikit_locale(self) -> str:
        return str(self.config.get("twikit_locale", "en-US") or "en-US").strip() or "en-US"

    def _twikit_proxy_url(self) -> str:
        return str(self.config.get("twikit_proxy_url", "http://172.19.0.1:7890") or "").strip()

    def _twikit_timeline_count(self) -> int:
        return max(1, int(self.config.get("twikit_timeline_count", 5) or 5))

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
        return str(self.config.get("image_proxy_url", "http://172.19.0.1:7890") or "").strip()

    def _image_download_timeout_seconds(self) -> int:
        return max(3, int(self.config.get("image_download_timeout_seconds", 20) or 20))

    def _max_image_bytes(self) -> int:
        return max(100_000, int(self.config.get("max_image_bytes", 8_000_000) or 8_000_000))

    def _failure_notice_threshold(self) -> int:
        return max(1, int(self.config.get("failure_notice_threshold", 6) or 6))

    def _translate_enabled(self) -> bool:
        return bool(self.config.get("translate_enabled", False))

    def _translate_target_lang(self) -> str:
        return str(self.config.get("translate_target_lang", "简体中文") or "简体中文").strip()

    def _translate_provider_id(self) -> str:
        return str(self.config.get("translate_provider_id", "") or "").strip()

    def _translate_show_original(self) -> bool:
        return bool(self.config.get("translate_show_original", True))

    def _translate_prompt(self) -> str:
        configured = str(self.config.get("translate_prompt", "") or "").strip()
        if configured:
            return configured.replace("{target_lang}", self._translate_target_lang())
        return (
            f"你是一个专业的翻译助手。请将用户提供的 X 推文翻译为{self._translate_target_lang()}。"
            "仅输出译文，不要添加解释、前缀、注释或原文对照。"
            "保留原文语气、换行、表情符号、话题标签和 @用户名。"
        )

    def _translate_timeout_seconds(self) -> int:
        return max(5, int(self.config.get("translate_timeout_seconds", 45) or 45))

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admin_ids = _split_ids(self.config.get("admin_user_ids", ""))
        return not admin_ids or str(event.get_sender_id()) in admin_ids

    async def _fetch_feed(self, handle: str) -> list[FeedItem]:
        if self._backend() == "rsshub":
            return await asyncio.to_thread(fetch_rsshub_user_feed, self._rsshub_base_url(), handle)

        if self._twikit_client is None:
            self._twikit_client = TwikitFeedClient(
                TwikitSettings(
                    cookies_file=self._twikit_cookies_file(),
                    locale=self._twikit_locale(),
                    proxy_url=self._twikit_proxy_url(),
                    timeline_count=self._twikit_timeline_count(),
                )
            )
        return await self._twikit_client.fetch_user_feed(handle)

    async def _send_to_origin(self, origin: str, text: str) -> None:
        if not origin:
            return
        await self.context.send_message(origin, MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]))

    async def _send_item_to_origin(self, origin: str, handle: str, item: FeedItem) -> None:
        await self._send_to_origin(origin, await self._format_item(handle, item, origin))
        await self._send_item_images_to_origin(origin, handle, item)

    async def _send_item_images_to_origin(self, origin: str, handle: str, item: FeedItem) -> None:
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
                logger.warning("X feed image send failed for @%s %s: %s", handle, item.link, error)

    async def _send_item_images_to_event(self, event: AstrMessageEvent, handle: str, item: FeedItem) -> None:
        if not self._include_images():
            return
        for image_url in item.image_urls[: self._max_images_per_post()]:
            try:
                image_path = await self._download_image(image_url)
                await event.send(MessageChain([Comp.Image.fromFileSystem(image_path)]))
            except Exception as error:
                logger.warning("X feed command image send failed for @%s %s: %s", handle, item.link, error)

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
                "User-Agent": "Mozilla/5.0 NightingaleOpsBot-XFeed/0.1",
                "Referer": "https://x.com/",
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
                logger.error("X feed poll loop error: %s", error)
            await asyncio.sleep(self._poll_interval_seconds())

    async def _poll_once(self) -> None:
        async with self._poll_lock:
            subscriptions = self.store.list_enabled()
            for subscription in subscriptions:
                await self._process_subscription(subscription)

    async def _process_subscription(self, subscription: Subscription) -> None:
        try:
            items = await self._fetch_feed(subscription.handle)
        except Exception as error:
            failure_count = self.store.update_failure(subscription.id, str(error))
            if failure_count >= self._failure_notice_threshold():
                logger.warning(
                    "X feed failed for @%s to %s for %s times: %s",
                    subscription.handle,
                    subscription.target_kind,
                    failure_count,
                    error,
                )
            else:
                logger.info("X feed failed for @%s: %s", subscription.handle, error)
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
            await self._send_item_to_origin(subscription.target_origin, subscription.handle, item)
            self.store.record_seen(
                handle=subscription.handle,
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

    async def _format_item(self, handle: str, item: FeedItem, origin: str = "") -> str:
        item_text = self._item_text(item)
        translated_text, translate_model = await self._translate_text(item_text, origin)

        lines = [f"X 更新：@{handle}"]
        if item_text:
            if translated_text and translate_model:
                if self._translate_show_original():
                    lines.extend(["原文：", item_text, "", f"{self._translate_target_lang()}：", translated_text])
                else:
                    lines.append(translated_text)
                lines.append(f"（由 {translate_model} 翻译）")
            else:
                lines.append(item_text)
        if item.published_at:
            lines.append(item.published_at)
        if item.link:
            lines.append(item.link)
        return "\n".join(lines)

    @staticmethod
    def _item_text(item: FeedItem) -> str:
        return str(item.title or item.summary or "").strip()

    async def _translate_text(self, text: str, origin: str = "") -> tuple[str, str | None]:
        text = str(text or "").strip()
        if not text or not self._translate_enabled():
            return text, None

        provider_id = await self._get_translate_provider_id(origin)
        if not provider_id:
            logger.warning("X feed translation skipped: no available LLM provider.")
            return text, None

        for attempt in range(2):
            try:
                response = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=text,
                        system_prompt=self._translate_prompt(),
                    ),
                    timeout=self._translate_timeout_seconds(),
                )
                translated = str(getattr(response, "completion_text", "") or "").strip()
                if translated:
                    return translated, self._provider_model_name(provider_id)
                logger.warning("X feed translation returned empty text, attempt %s.", attempt + 1)
            except Exception as error:
                logger.warning("X feed translation failed, attempt %s: %s", attempt + 1, error)
            if attempt == 0:
                await asyncio.sleep(1)

        return text, None

    async def _get_translate_provider_id(self, origin: str = "") -> str | None:
        configured = self._translate_provider_id()
        if configured:
            try:
                if self.context.get_provider_by_id(configured):
                    return configured
            except Exception as error:
                logger.warning("X feed configured translation provider lookup failed: %s", error)

        if origin:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=origin)
                if provider_id:
                    return str(provider_id)
            except Exception as error:
                logger.warning("X feed current chat provider lookup failed: %s", error)

        try:
            providers = self.context.get_all_providers()
            if providers:
                return str(providers[0].meta().id)
        except Exception as error:
            logger.warning("X feed provider list lookup failed: %s", error)

        return None

    def _provider_model_name(self, provider_id: str) -> str:
        try:
            provider = self.context.get_provider_by_id(provider_id)
            if provider and hasattr(provider, "meta"):
                meta = provider.meta()
                model_name = getattr(meta, "model_name", "") or ""
                return str(model_name or provider_id)
        except Exception:
            pass
        return provider_id

    @filter.command("x帮助")
    async def x_help(self, event: AstrMessageEvent):
        yield event.plain_result(_help_text())

    @filter.command("x翻译状态")
    async def x_translation_status(self, event: AstrMessageEvent):
        status = "开启" if self._translate_enabled() else "关闭"
        provider = self._translate_provider_id() or "自动选择"
        original = "保留原文" if self._translate_show_original() else "只发译文"
        yield event.plain_result(
            "\n".join(
                [
                    f"X 推送翻译：{status}",
                    f"目标语言：{self._translate_target_lang()}",
                    f"Provider：{provider}",
                    f"模式：{original}",
                ]
            )
        )

    @filter.command("x订阅")
    async def subscribe_x(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("你没有管理 X 推送订阅的权限。")
            return

        origin = _event_origin(event)
        if not origin:
            yield event.plain_result("当前会话来源无法记录，请稍后重试。")
            return

        try:
            handle = normalize_handle(_command_argument(event.message_str or "", ("x订阅",)))
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        try:
            items = await self._fetch_feed(handle)
        except Exception as error:
            yield event.plain_result(
                _clamp(
                    f"订阅前测试失败：{error}\n\n当前后端：{self._backend_label()}。",
                    self.max_output_chars,
                )
            )
            return

        latest = items[0] if items else None
        subscription, created = self.store.upsert_subscription(
            handle=handle,
            target_origin=origin,
            target_kind=_target_kind(event),
            created_by=str(event.get_sender_id()),
            last_seen_id=latest.item_id if latest else "",
            last_seen_link=latest.link if latest else "",
        )

        lines = [f"已订阅 @{subscription.handle}。" if created else f"@{subscription.handle} 已恢复订阅。"]
        backfill = self._initial_backfill_items()
        if latest and backfill > 0:
            lines.append("")
            lines.append("当前最新：")
            for item in items[:backfill]:
                lines.append(await self._format_item(subscription.handle, item, origin))
                self.store.record_seen(
                    handle=subscription.handle,
                    item_id=item.item_id,
                    link=item.link,
                    published_at=item.published_at,
                )
        if latest and self._include_images() and latest.image_urls:
            await event.send(MessageChain([Comp.Plain(_clamp("\n\n".join(lines), self.max_output_chars))]))
            await self._send_item_images_to_event(event, subscription.handle, latest)
            return
        yield event.plain_result(_clamp("\n\n".join(lines), self.max_output_chars))

    @filter.command("x取消订阅")
    async def unsubscribe_x(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("你没有管理 X 推送订阅的权限。")
            return
        origin = _event_origin(event)
        try:
            handle = normalize_handle(_command_argument(event.message_str or "", ("x取消订阅",)))
        except ValueError as error:
            yield event.plain_result(str(error))
            return
        removed = self.store.remove_subscription(handle=handle, target_origin=origin)
        yield event.plain_result(f"已取消订阅 @{handle}。" if removed else f"当前会话没有订阅 @{handle}。")

    @filter.command("x订阅列表")
    async def list_x_subscriptions(self, event: AstrMessageEvent):
        origin = _event_origin(event)
        subscriptions = self.store.list_for_origin(origin)
        if not subscriptions:
            yield event.plain_result("当前会话还没有 X 订阅。")
            return
        lines = ["当前会话的 X 订阅："]
        for subscription in subscriptions:
            status = "开启" if subscription.enabled else "暂停"
            last = subscription.last_success_at or "-"
            fail = f"，连续失败 {subscription.failure_count} 次" if subscription.failure_count else ""
            lines.append(f"- @{subscription.handle}：{status}，上次成功 {last}{fail}")
        yield event.plain_result(_clamp("\n".join(lines), self.max_output_chars))

    @filter.command("x推送测试")
    async def test_x_feed(self, event: AstrMessageEvent):
        try:
            handle = normalize_handle(_command_argument(event.message_str or "", ("x推送测试",)))
        except ValueError as error:
            yield event.plain_result(str(error))
            return
        try:
            items = await self._fetch_feed(handle)
        except Exception as error:
            yield event.plain_result(
                _clamp(f"测试失败：{error}\n\n当前后端：{self._backend_label()}。", self.max_output_chars)
            )
            return
        if not items:
            yield event.plain_result(f"@{handle} 当前没有可推送条目。")
            return
        text = _clamp(
            "测试成功：\n" + await self._format_item(handle, items[0], _event_origin(event)),
            self.max_output_chars,
        )
        if self._include_images() and items[0].image_urls:
            await event.send(MessageChain([Comp.Plain(text)]))
            await self._send_item_images_to_event(event, handle, items[0])
            return
        yield event.plain_result(text)

    @filter.command("x推送开")
    async def enable_x_feed(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("你没有管理 X 推送订阅的权限。")
            return
        count = self.store.set_origin_enabled(_event_origin(event), True)
        yield event.plain_result(f"已开启当前会话的 X 推送订阅：{count} 个。")

    @filter.command("x推送关")
    async def disable_x_feed(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("你没有管理 X 推送订阅的权限。")
            return
        count = self.store.set_origin_enabled(_event_origin(event), False)
        yield event.plain_result(f"已暂停当前会话的 X 推送订阅：{count} 个。")
