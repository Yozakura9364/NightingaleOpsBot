from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .sources import SOURCES, WatchItem, fetch_source, list_sources, source_ids_for_kind
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
            "商城测试当前只是详情页样例，不代表新品上架。",
            "当前源：/ff14watch 源",
            "关闭单源：/ff14watch 源 jp-news off",
            "开启单源：/ff14watch 源 jp-news on",
            "关闭当前会话：/ff14watch 关",
            "开启当前会话：/ff14watch 开",
            "重建基线：/ff14watch 基线",
            "",
            "当前新闻源：cn-news / cn-notice / jp-news",
            "当前商城源：cn-store / tw-store / jp-store",
            "韩服商城暂不启用。首次轮询只建立基线，不会推历史。",
            "当前新品提醒建议优先订阅新闻；商城源暂为详情页变更监控。",
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
        self.store = FFXIVWatchStore(self.data_dir)
        self.max_output_chars = int(self.config.get("max_output_chars", 3000) or 3000)
        self._poll_task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()

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
        return max(10, int(self.config.get("poll_interval_minutes", 60) or 60)) * 60

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

    def _enabled_source_ids(self) -> list[str]:
        configured = _split_ids(self.config.get("enabled_sources", "cn-news,cn-notice,jp-news,cn-store,tw-store,jp-store"))
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
            for source_id in self._enabled_source_ids():
                source = SOURCES[source_id]
                targets = self.store.targets_for_source(source_id=source_id, category=source.kind)
                if not targets:
                    continue
                await self._process_source(source_id, targets)

    async def _process_source(self, source_id: str, targets: list[str]) -> None:
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
        if not state.baseline_done:
            for item in items:
                self._record_event(item)
            self.store.record_source_success(source_id=source_id, keys=keys, baseline_done=True)
            logger.info("FF14 watch source %s baseline recorded with %s items.", source_id, len(items))
            return

        old_keys = set(state.last_keys)
        new_items = [item for item in items if item.stable_key() not in old_keys]
        if not new_items:
            self.store.record_source_success(source_id=source_id, keys=keys, baseline_done=True)
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
        self.store.record_source_success(source_id=source_id, keys=keys, baseline_done=True)

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

    async def _send_item_to_origin(self, origin: str, text: str, item: WatchItem) -> None:
        await self._send_to_origin(origin, text)
        await self._send_item_images_to_origin(origin, item)

    async def _send_item_images_to_origin(self, origin: str, item: WatchItem) -> None:
        if not origin or not self._include_images() or not item.image or self._max_images_per_item() <= 0:
            return
        try:
            await self.context.send_message(origin, MessageChain([Comp.Image.fromURL(item.image)]))
        except Exception as error:
            logger.warning("FF14 watch image send failed for %s: %s", item.url, error)

    async def _send_item_images_to_event(self, event: AstrMessageEvent, item: WatchItem) -> None:
        if not self._include_images() or not item.image or self._max_images_per_item() <= 0:
            return
        try:
            await event.send(MessageChain([Comp.Image.fromURL(item.image)]))
        except Exception as error:
            logger.warning("FF14 watch command image send failed for %s: %s", item.url, error)

    def _format_item(self, item: WatchItem) -> str:
        kind_label = "新闻更新" if item.kind == "news" else "商城详情变更"
        lines = [f"FF14 {kind_label} - {item.region}"]
        if item.category:
            lines.append(f"分类：{item.category}")
        lines.append(item.title)
        if item.price:
            price = f"{item.price} {item.currency}".strip()
            lines.append(f"价格：{price}")
        if item.summary:
            lines.append(item.summary)
        if item.published_at:
            lines.append(f"时间：{item.published_at}")
        if item.url:
            lines.append(item.url)
        return "\n".join(lines)

    def _format_items_for_test(self, source_id: str, items: list[WatchItem]) -> str:
        source = SOURCES[source_id]
        if not items:
            return f"{source.label}：没有抓到条目。"
        if source.kind == "store":
            lines = [f"{source.label}：详情页监控样例，抓到 {len(items)} 条，显示前 3 条"]
            lines.append("说明：这不是新品上架列表，只用于验证当前详情页解析和配图。")
        else:
            lines = [f"{source.label}：抓到 {len(items)} 条，显示前 3 条"]
        for item in items[:3]:
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
            lines: list[str] = []
            first_image_item: WatchItem | None = None
            for source_id in source_ids:
                try:
                    items = await asyncio.to_thread(
                        fetch_source,
                        source_id,
                        self._request_timeout_seconds(),
                        self._rsshub_base_url(),
                    )
                    lines.append(self._format_items_for_test(source_id, items))
                    if not first_image_item:
                        first_image_item = next((item for item in items if item.image), None)
                except Exception as error:
                    lines.append(f"{source_id} 测试失败：{error}")
            await event.send(MessageChain([Comp.Plain(_clamp("\n\n".join(lines), self.max_output_chars))]))
            if first_image_item:
                await self._send_item_images_to_event(event, first_image_item)
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
        lines = [
            "FF14 watch 状态",
            f"插件后台：{'开启' if self.config.get('enabled', True) else '关闭'}",
            f"当前会话：{'开启' if target_enabled else '关闭'}",
            f"轮询间隔：{self._poll_interval_seconds() // 60} 分钟",
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
