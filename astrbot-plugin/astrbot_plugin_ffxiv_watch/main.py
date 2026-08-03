from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, build_opener

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .sources import SOURCES, USER_AGENT, WatchItem, fetch_source, list_sources, source_ids_for_kind
from .storage import FFXIVWatchStore


COMMANDS = ("ff14watch",)
CATEGORY_ALIASES = {
    "news": "news",
    "新闻": "news",
    "公告": "news",
    "store": "store",
    "商城": "store",
    "商店": "store",
}
CATEGORY_LABELS = {
    "news": "新闻",
    "store": "商城",
}
STORE_TIMEZONE = timezone(timedelta(hours=8))


def _split_ids(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return {part.strip() for part in str(value).replace("\n", ",").split(",") if part.strip()}


def _event_origin(event: AstrMessageEvent) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "").strip()


def _target_kind(event: AstrMessageEvent) -> str:
    return "group" if str(event.get_group_id() or "").strip() else "private"


def _strip_command(text: str, commands: tuple[str, ...]) -> str:
    first_line = str(text or "").strip().splitlines()[0].strip() if str(text or "").strip() else ""
    if first_line.startswith("/"):
        first_line = first_line[1:].lstrip()
    for command in commands:
        if first_line == command:
            return ""
        if first_line.startswith(command + " "):
            return first_line[len(command) :].strip()
    return first_line


def _clamp(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 16)] + "\n...[已截断]"


def _category_from_text(value: str) -> str:
    category = CATEGORY_ALIASES.get(str(value or "").strip().lower())
    if not category:
        raise ValueError("分类只能是：新闻 / 商城。")
    return category


def _help_text() -> str:
    return "\n".join(
        [
            "FF14 官方更新提醒",
            "",
            "订阅新闻：/ff14watch 订阅 新闻",
            "订阅商城：/ff14watch 订阅 商城",
            "取消订阅：/ff14watch 取消 新闻",
            "查看订阅：/ff14watch 订阅列表",
            "当前状态：/ff14watch 状态",
            "测试抓取：/ff14watch 测试",
            "测试单源：/ff14watch 测试 jp-news",
            "测试商城新品源：/ff14watch 测试 cn-store",
            "当前源：/ff14watch 源",
            "关闭单源：/ff14watch 源 jp-news off",
            "开启单源：/ff14watch 源 jp-news on",
            "关闭当前会话：/ff14watch 关",
            "开启当前会话：/ff14watch 开",
            "重建基线：/ff14watch 基线",
            "",
            "当前新闻源：cn-news / cn-notice / jp-news / na-news",
            "当前商城源：cn-store / tw-store / jp-store",
            "韩服商城暂不启用。首次轮询只建立基线，不会推历史。",
            "商城源监控商品列表，只在发现新 SKU 时推送。",
        ]
    )


@register(
    "astrbot_plugin_ffxiv_watch",
    "NightingaleSilence",
    "FF14 official news and store update watcher.",
    "0.1.0",
)
class FFXIVWatchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(__file__).resolve().parent / ".local"
        self.image_dir = self.data_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.store = FFXIVWatchStore(self.data_dir)
        self.max_output_chars = int(self.config.get("max_output_chars", 3000) or 3000)
        self._poll_task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()
        self._last_store_poll_slot = ""

    async def initialize(self) -> None:
        if self.config.get("enabled", True):
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("FF14 watch poll loop started.")

    async def terminate(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    def _poll_interval_seconds(self) -> int:
        return max(15, int(self.config.get("news_poll_interval_seconds", 30) or 30))

    def _store_poll_hours(self) -> tuple[int, int]:
        start_hour = max(0, min(23, int(self.config.get("store_poll_start_hour", 6) or 6)))
        end_hour = max(start_hour, min(23, int(self.config.get("store_poll_end_hour", 20) or 20)))
        return start_hour, end_hour

    def _store_poll_minutes(self) -> tuple[int, ...]:
        values = sorted(
            {
                int(value)
                for value in _split_ids(self.config.get("store_poll_minutes", "5,10"))
                if value.isdigit() and 0 <= int(value) <= 59
            }
        )
        return tuple(values or (5, 10))

    def _current_store_poll_slot(self, now: datetime | None = None) -> str:
        current = (now or datetime.now(STORE_TIMEZONE)).astimezone(STORE_TIMEZONE)
        start_hour, end_hour = self._store_poll_hours()
        if not start_hour <= current.hour <= end_hour:
            return ""
        if current.minute not in self._store_poll_minutes():
            return ""
        return current.strftime("%Y-%m-%dT%H:%M%z")

    def _claim_store_poll_slot(self, now: datetime | None = None) -> bool:
        slot = self._current_store_poll_slot(now)
        if not slot or slot == self._last_store_poll_slot:
            return False
        self._last_store_poll_slot = slot
        return True

    def _startup_delay_seconds(self) -> int:
        return max(5, int(self.config.get("startup_delay_seconds", 30) or 30))

    def _request_timeout_seconds(self) -> int:
        return max(5, int(self.config.get("request_timeout_seconds", 20) or 20))

    def _rsshub_base_url(self) -> str:
        return str(self.config.get("rsshub_base_url", "http://rsshub:1200") or "http://rsshub:1200").rstrip("/")

    def _max_items_per_poll(self) -> int:
        return max(1, int(self.config.get("max_items_per_poll", 5) or 5))

    def _include_images(self) -> bool:
        return bool(self.config.get("include_images", True))

    def _max_images_per_item(self) -> int:
        return max(0, int(self.config.get("max_images_per_item", 1) or 0))

    def _failure_notice_threshold(self) -> int:
        return max(1, int(self.config.get("failure_notice_threshold", 3) or 3))

    def _image_download_timeout_seconds(self) -> int:
        return max(5, min(30, self._request_timeout_seconds()))

    def _max_image_bytes(self) -> int:
        return 8 * 1024 * 1024

    def _enabled_source_ids(self) -> list[str]:
        configured = _split_ids(self.config.get("enabled_sources", "cn-news,cn-notice,jp-news,na-news,cn-store,tw-store,jp-store"))
        if not configured:
            configured = set(SOURCES.keys())
        return [source_id for source_id in SOURCES if source_id in configured]

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id())
        manager_ids = _split_ids(self.config.get("manager_user_ids", ""))
        if sender in manager_ids:
            return True
        if not self.config.get("manage_requires_admin", True):
            return True
        return bool(event.is_admin())

    async def _poll_loop(self) -> None:
        await asyncio.sleep(self._startup_delay_seconds())
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("FF14 watch poll loop error: %s", error)
            await asyncio.sleep(self._poll_interval_seconds())

    async def _poll_once(self) -> None:
        async with self._poll_lock:
            store_due = self._claim_store_poll_slot()
            for source_id in self._enabled_source_ids():
                source = SOURCES[source_id]
                if source.kind == "store" and not store_due:
                    continue
                targets = self.store.targets_for_source(source_id=source_id, category=source.kind)
                if not targets:
                    continue
                await self._process_source(source_id, targets)

    async def _process_source(self, source_id: str, targets: list[str]) -> None:
        source = SOURCES[source_id]
        state = self.store.get_source_state(source_id)
        try:
            items = await asyncio.to_thread(
                fetch_source,
                source_id,
                self._request_timeout_seconds(),
                self._rsshub_base_url(),
            )
        except Exception as error:
            failure_count = self.store.record_source_failure(source_id=source_id, error=str(error))
            if failure_count >= self._failure_notice_threshold():
                logger.warning("FF14 watch source %s failed %s times: %s", source_id, failure_count, error)
            else:
                logger.info("FF14 watch source %s failed: %s", source_id, error)
            return

        keys = [item.stable_key() for item in items]
        needs_baseline = not state.baseline_done or state.baseline_version != source.baseline_version
        if needs_baseline:
            for item in items:
                self._record_event(item)
            self.store.record_source_success(
                source_id=source_id,
                keys=keys,
                baseline_done=True,
                baseline_version=source.baseline_version,
            )
            logger.info(
                "FF14 watch source %s baseline v%s recorded with %s items.",
                source_id,
                source.baseline_version,
                len(items),
            )
            return

        old_keys = set(state.last_keys)
        new_items = [item for item in items if item.stable_key() not in old_keys]
        if not new_items:
            self.store.record_source_success(
                source_id=source_id,
                keys=keys,
                baseline_done=True,
                baseline_version=source.baseline_version,
            )
            return

        for item in reversed(new_items[: self._max_items_per_poll()]):
            event_key = item.stable_key()
            created = self._record_event(item)
            if not created:
                continue
            text = self._format_item(item)
            for target_origin in targets:
                if not self.store.mark_delivered(event_key=event_key, target_origin=target_origin):
                    continue
                await self._send_item_to_origin(target_origin, text, item)
        self.store.record_source_success(
            source_id=source_id,
            keys=keys,
            baseline_done=True,
            baseline_version=source.baseline_version,
        )

    def _record_event(self, item: WatchItem) -> bool:
        return self.store.upsert_event(
            event_key=item.stable_key(),
            source_id=item.source_id,
            kind=item.kind,
            title=item.title,
            url=item.url,
            published_at=item.published_at,
            payload=item.payload(),
        )

    async def _send_to_origin(self, origin: str, text: str) -> None:
        await self.context.send_message(origin, MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]))

    def _can_inline_image(self, item: WatchItem | None) -> bool:
        return bool(item and self._include_images() and item.image and self._max_images_per_item() > 0)

    def _build_item_message_chain(
        self,
        text: str,
        image_path: str = "",
        item: WatchItem | None = None,
    ) -> MessageChain:
        if not image_path:
            return MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))])
        if item is None or text != self._format_item(item):
            return MessageChain(
                [Comp.Plain(_clamp(text, self.max_output_chars)), Comp.Image.fromFileSystem(image_path)]
            )

        before_image, after_image = self._format_item_parts(item)
        components: list = [Comp.Plain(_clamp(before_image, self.max_output_chars))]
        components.append(Comp.Image.fromFileSystem(image_path))
        if after_image:
            components.append(Comp.Plain(_clamp(after_image, self.max_output_chars)))
        return MessageChain(components)

    async def _download_item_image(self, item: WatchItem | None) -> str:
        if not self._can_inline_image(item):
            return ""
        return await asyncio.to_thread(self._download_item_image_sync, item.image)

    def _download_item_image_sync(self, image_url: str) -> str:
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("invalid image url scheme")

        # 国服商城图片 URL 的路径/查询可能含中文文件名（如 战场玫瑰套装….jpg），
        # urllib 构造 Request 时对非 ASCII 字符按 ascii 编码会抛 UnicodeEncodeError，
        # 这里先把非 ASCII 部分做百分号编码（保留已有转义与常见保留字符）。
        encoded_url = urlunparse(
            parsed._replace(
                path=quote(parsed.path, safe="/%"),
                query=quote(parsed.query, safe="=&%"),
            )
        )

        suffix = Path(parsed.path).suffix.lower()
        extension = suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"} else ".jpg"
        if extension == ".jpeg":
            extension = ".jpg"
        target = self.image_dir / f"{sha256(encoded_url.encode('utf-8')).hexdigest()[:24]}{extension}"
        if target.exists() and 0 < target.stat().st_size <= self._max_image_bytes():
            return str(target)

        request = Request(encoded_url, headers={"User-Agent": USER_AGENT})
        opener = build_opener()
        try:
            with opener.open(request, timeout=self._image_download_timeout_seconds()) as response:
                content_type = str(response.headers.get("Content-Type", ""))
                if not content_type.lower().startswith("image/"):
                    raise RuntimeError(f"unexpected image content type: {content_type}")
                data = response.read(self._max_image_bytes() + 1)
        except URLError as error:
            raise RuntimeError(f"image download failed: {error.reason}") from error

        if not data:
            raise RuntimeError("image payload is empty")
        if len(data) > self._max_image_bytes():
            raise RuntimeError("image payload is too large")

        target.write_bytes(data)
        return str(target)

    async def _send_image_path_to_origin(self, origin: str, image_path: str) -> None:
        if not origin or not image_path:
            return
        await self.context.send_message(origin, MessageChain([Comp.Image.fromFileSystem(image_path)]))

    async def _send_image_path_to_event(self, event: AstrMessageEvent, image_path: str) -> None:
        if not image_path:
            return
        await event.send(MessageChain([Comp.Image.fromFileSystem(image_path)]))

    async def _send_item_to_origin(self, origin: str, text: str, item: WatchItem) -> None:
        if not origin:
            return
        image_path = ""
        if self._can_inline_image(item):
            try:
                image_path = await self._download_item_image(item)
            except Exception as error:
                logger.warning("FF14 watch image download failed for %s: %s", item.url, error)
        try:
            await self.context.send_message(origin, self._build_item_message_chain(text, image_path, item))
        except Exception as error:
            if image_path:
                logger.warning("FF14 watch mixed send failed for %s: %s", item.url, error)
                await self._send_to_origin(origin, text)
                await self._send_image_path_to_origin(origin, image_path)
                return
            raise

    async def _send_item_to_event(self, event: AstrMessageEvent, text: str, item: WatchItem | None = None) -> None:
        image_path = ""
        if self._can_inline_image(item):
            try:
                image_path = await self._download_item_image(item)
            except Exception as error:
                logger.warning("FF14 watch command image download failed for %s: %s", item.url, error)
        try:
            await event.send(self._build_item_message_chain(text, image_path, item))
        except Exception as error:
            if image_path:
                logger.warning("FF14 watch command mixed send failed for %s: %s", item.url, error)
                await event.send(MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]))
                await self._send_image_path_to_event(event, image_path)
                return
            raise

    def _format_item(self, item: WatchItem) -> str:
        before_image, after_image = self._format_item_parts(item)
        return "\n".join(part for part in (before_image, after_image) if part)

    def _format_item_parts(self, item: WatchItem) -> tuple[str, str]:
        kind_label = "新闻更新" if item.kind == "news" else "商城上新"
        before_lines = [f"FF14 {kind_label} - {item.region}"]
        if item.category:
            before_lines.append(f"分类：{item.category}")
        before_lines.append(item.title)

        after_lines = []
        if item.price:
            price = f"{item.price} {item.currency}".strip()
            after_lines.append(f"价格：{price}")
        if item.summary:
            after_lines.append(item.summary)
        if item.published_at:
            after_lines.append(f"时间：{item.published_at}")
        if item.url:
            after_lines.append(item.url)
        return "\n".join(before_lines), "\n".join(after_lines)

    def _format_items_for_test(self, source_id: str, items: list[WatchItem]) -> str:
        source = SOURCES[source_id]
        if not items:
            return f"{source.label}：没有抓到条目。"
        if source.kind == "store":
            lines = [f"{source.label}：新品列表抓到 {len(items)} 条，显示前 1 条"]
            lines.append("说明：测试只展示抓取结果，不写入推送基线。")
        else:
            lines = [f"{source.label}：抓到 {len(items)} 条，显示前 1 条"]
        for item in items[:1]:
            lines.append("")
            lines.append(self._format_item(item))
        return "\n".join(lines)

    async def _baseline_sources(self, source_ids: list[str]) -> tuple[int, list[str]]:
        ok = 0
        errors: list[str] = []
        for source_id in source_ids:
            try:
                items = await asyncio.to_thread(
                    fetch_source,
                    source_id,
                    self._request_timeout_seconds(),
                    self._rsshub_base_url(),
                )
                for item in items:
                    self._record_event(item)
                self.store.record_source_success(
                    source_id=source_id,
                    keys=[item.stable_key() for item in items],
                    baseline_done=True,
                    baseline_version=SOURCES[source_id].baseline_version,
                )
                ok += 1
            except Exception as error:
                self.store.record_source_failure(source_id=source_id, error=str(error))
                errors.append(f"{source_id}: {error}")
        return ok, errors

    async def _handle(self, event: AstrMessageEvent):
        origin = _event_origin(event)
        if not origin:
            yield event.plain_result("当前会话来源无法记录，请稍后重试。")
            return

        target_kind = _target_kind(event)
        remainder = _strip_command(event.message_str or "", COMMANDS)
        parts = remainder.split(maxsplit=2)
        sub = parts[0].strip().lower() if parts else ""
        first_arg = parts[1].strip() if len(parts) > 1 else ""
        rest = parts[2].strip() if len(parts) > 2 else ""

        if not sub or sub in {"help", "帮助", "菜单"}:
            yield event.plain_result(_help_text())
            return

        if sub in {"status", "状态"}:
            yield event.plain_result(_clamp(self._format_status(origin), self.max_output_chars))
            return

        if sub in {"list", "订阅列表", "列表"}:
            yield event.plain_result(_clamp(self._format_subscriptions(origin), self.max_output_chars))
            return

        if sub in {"test", "测试"}:
            source_ids = [first_arg] if first_arg else self._enabled_source_ids()
            invalid = [source_id for source_id in source_ids if source_id not in SOURCES]
            if invalid:
                yield event.plain_result(f"未知数据源：{', '.join(invalid)}")
                return
            yield event.plain_result(f"正在测试 FF14 数据源（{len(source_ids)} 个），请稍候。")
            tasks = [
                asyncio.to_thread(
                    fetch_source,
                    source_id,
                    self._request_timeout_seconds(),
                    self._rsshub_base_url(),
                )
                for source_id in source_ids
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            lines: list[str] = []
            first_image_item: WatchItem | None = None
            for source_id, result in zip(source_ids, results):
                if isinstance(result, BaseException):
                    lines.append(f"{source_id} 测试失败：{result}")
                    continue
                lines.append(self._format_items_for_test(source_id, result))
                if not first_image_item:
                    first_image_item = next((item for item in result if item.image), None)
            await self._send_item_to_event(event, "\n\n".join(lines), first_image_item)
            return

        if sub in {"source", "源", "来源"}:
            async for result in self._handle_source_command(event, origin, target_kind, first_arg, rest):
                yield result
            return

        if not self._can_manage(event):
            yield event.plain_result("权限不足：FF14 watch 管理命令仅限管理员使用。")
            return

        if sub in {"subscribe", "订阅"}:
            try:
                category = _category_from_text(first_arg)
            except ValueError as error:
                yield event.plain_result(str(error))
                return
            source_ids = [source_id for source_id in source_ids_for_kind(category) if source_id in self._enabled_source_ids()]
            created = self.store.subscribe(
                target_origin=origin,
                target_kind=target_kind,
                category=category,
                created_by=str(event.get_sender_id()),
                source_ids=source_ids,
            )
            label = CATEGORY_LABELS[category]
            yield event.plain_result(
                f"已订阅 FF14 {label}提醒。" if created else f"已恢复 FF14 {label}提醒。"
                "\n首次轮询只建立基线，不会推历史内容。"
            )
            return

        if sub in {"unsubscribe", "取消", "取消订阅"}:
            try:
                category = _category_from_text(first_arg)
            except ValueError as error:
                yield event.plain_result(str(error))
                return
            removed = self.store.unsubscribe(target_origin=origin, category=category)
            label = CATEGORY_LABELS[category]
            yield event.plain_result(f"已取消 FF14 {label}提醒。" if removed else f"当前会话没有订阅 FF14 {label}提醒。")
            return

        if sub in {"on", "开", "开启"}:
            self.store.set_target_enabled(target_origin=origin, target_kind=target_kind, enabled=True)
            yield event.plain_result("已开启当前会话的 FF14 watch 推送。")
            return

        if sub in {"off", "关", "关闭"}:
            self.store.set_target_enabled(target_origin=origin, target_kind=target_kind, enabled=False)
            yield event.plain_result("已关闭当前会话的 FF14 watch 推送；订阅记录不会删除。")
            return

        if sub in {"baseline", "基线"}:
            ok, errors = await self._baseline_sources(self._enabled_source_ids())
            lines = [f"FF14 watch 基线完成：{ok} 个源成功。"]
            if errors:
                lines.append("失败：")
                lines.extend(errors[:5])
            yield event.plain_result(_clamp("\n".join(lines), self.max_output_chars))
            return

        yield event.plain_result(_help_text())

    async def _handle_source_command(
        self,
        event: AstrMessageEvent,
        origin: str,
        target_kind: str,
        source_id: str,
        action: str,
    ):
        if not source_id:
            yield event.plain_result(_clamp(self._format_sources(origin), self.max_output_chars))
            return
        if source_id not in SOURCES:
            yield event.plain_result(f"未知数据源：{source_id}")
            return
        action = action.strip().lower()
        if action not in {"on", "off", "开", "关", "开启", "关闭"}:
            yield event.plain_result("用法：/ff14watch 源 jp-news off")
            return
        if not self._can_manage(event):
            yield event.plain_result("权限不足：数据源开关仅限管理员使用。")
            return
        enabled = action in {"on", "开", "开启"}
        self.store.set_source_enabled(
            target_origin=origin,
            target_kind=target_kind,
            source_id=source_id,
            enabled=enabled,
        )
        yield event.plain_result(f"当前会话已{'开启' if enabled else '关闭'} {source_id}。")

    def _format_status(self, origin: str) -> str:
        target = self.store.get_target(origin)
        target_enabled = target.enabled if target else True
        subscriptions = self.store.list_subscriptions(origin)
        store_start_hour, store_end_hour = self._store_poll_hours()
        store_minutes = self._store_poll_minutes()
        lines = [
            "FF14 watch 状态",
            f"插件后台：{'开启' if self.config.get('enabled', True) else '关闭'}",
            f"当前会话：{'开启' if target_enabled else '关闭'}",
            f"新闻轮询：{self._poll_interval_seconds()} 秒",
            (
                f"商城轮询：北京时间 {store_start_hour:02d}:{store_minutes[0]:02d}-"
                f"{store_end_hour:02d}:{store_minutes[-1]:02d}，每小时 "
                f"{'/'.join(f'{minute:02d}' for minute in store_minutes)} 分"
            ),
            f"配图：{'开启' if self._include_images() else '关闭'}",
            f"全局源：{', '.join(self._enabled_source_ids())}",
            f"全局订阅会话数：{self.store.count_deliverable_targets()}",
            "",
            self._format_subscriptions(origin),
            "",
            self._format_sources(origin),
        ]
        return "\n".join(lines)

    def _format_subscriptions(self, origin: str) -> str:
        subscriptions = self.store.list_subscriptions(origin)
        if not subscriptions:
            return "当前会话还没有 FF14 watch 订阅。"
        lines = ["当前会话订阅："]
        for subscription in subscriptions:
            label = CATEGORY_LABELS.get(subscription.category, subscription.category)
            lines.append(f"- {label}：{'开启' if subscription.enabled else '暂停'}")
        return "\n".join(lines)

    def _format_sources(self, origin: str) -> str:
        settings = self.store.list_target_source_settings(origin)
        global_enabled = set(self._enabled_source_ids())
        lines = ["当前会话数据源："]
        for source in list_sources():
            if source.id not in global_enabled:
                status = "全局关闭"
            else:
                status = "开启" if settings.get(source.id, True) else "关闭"
            lines.append(f"- {source.id}：{status}，{source.region} / {source.label}")
        return "\n".join(lines)

    @filter.command("ff14watch")
    async def ff14watch(self, event: AstrMessageEvent):
        async for result in self._handle(event):
            yield result
