from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
import re
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .storage import DeadlineItem, DeadlineStore

# ── parsing helpers ──────────────────────────────────────────────

TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
DATE_TIME_DASH_RE = re.compile(r"^(\d{4}-\d{1,2}-\d{1,2})-(\d{1,2}:\d{2})$")
URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# Hermes runtime endpoint
RUNTIME_URL = "https://www.nightingalesilence.com/data/runtime/ffxiv/community-events.json"


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


def _is_group(event: AstrMessageEvent) -> bool:
    return _target_kind(event) == "group"


def _clamp(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 16)] + "\n...[已截断]"


def _strip_command(text: str, commands: tuple[str, ...]) -> str:
    first_line = str(text or "").strip().splitlines()[0].strip() if str(text or "").strip() else ""
    if first_line.startswith("/"):
        first_line = first_line[1:].lstrip()
    for command in commands:
        if first_line == command:
            return ""
        if first_line.startswith(command + " "):
            return first_line[len(command):].strip()
    return first_line


def _help_text(hour: int, minute: int) -> str:
    return "\n".join(
        [
            "日程提醒（Hermes 同步）",
            "",
            f"每天 {hour:02d}:{minute:02d} 推送当前会话未结束的 DDL。",
            "活动由 Hermes 自动同步，无需手动添加。",
            "活动开始时会推送开始提醒（可在插件配置关闭）。",
            "",
            "查看：/ddl 列表",
            "预览：/ddl 今日",
            "手动同步：/ddl 同步",
            "",
            "关闭当前会话推送：/ddl 关",
            "开启当前会话推送：/ddl 开",
            "",
            "广播加入当前群：/ddl 广播加入",
            "查看广播日程：/ddl 广播列表",
            "预览广播日程：/ddl 广播今日",
            "",
            "添加/修改活动请在飞书告诉 Hermes。",
        ]
    )


@register(
    "astrbot_plugin_deadline_reminder",
    "NightingaleSilence",
    "Auto-synced deadline reminder fed by Hermes community events.",
    "0.3.0",
)
class DeadlineReminderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(__file__).resolve().parent / ".local"
        self.store = DeadlineStore(self.data_dir)
        self.max_output_chars = int(self.config.get("max_output_chars", 3000) or 3000)
        self._daily_task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        if self.config.get("enabled", True):
            self._daily_task = asyncio.create_task(self._daily_loop())
            self._sync_task = asyncio.create_task(self._auto_sync_loop())
            logger.info("Deadline reminder started (Hermes sync).")

    async def terminate(self) -> None:
        for task in (self._daily_task, self._sync_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def _timezone(self) -> str:
        return str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai")

    def _daily_hour(self) -> int:
        return max(0, min(23, int(self.config.get("daily_hour", 9) or 9)))

    def _daily_minute(self) -> int:
        return max(0, min(59, int(self.config.get("daily_minute", 0) or 0)))

    def _notify_on_start(self) -> bool:
        return bool(self.config.get("notify_on_start", True))

    def _start_advance_days(self) -> int:
        try:
            days = int(self.config.get("start_advance_days", 0) or 0)
        except (TypeError, ValueError):
            days = 0
        return max(0, min(30, days))

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id())
        manager_ids = _split_ids(self.config.get("manager_user_ids", ""))
        if sender in manager_ids:
            return True
        if not self.config.get("manage_requires_admin", True):
            return True
        return bool(event.is_admin())

    # ── sync ──────────────────────────────────────────────

    async def _auto_sync_loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                await self._sync_from_hermes()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Hermes sync loop error: %s", error)
            await asyncio.sleep(1800)  # 30 min

    async def _fetch_runtime_json(self) -> dict:
        try:
            import aiohttp
        except ImportError:
            import urllib.request, json
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: json.loads(urllib.request.urlopen(RUNTIME_URL, timeout=15).read()))
        async with aiohttp.ClientSession() as session:
            async with session.get(RUNTIME_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                return await resp.json()

    async def _sync_from_hermes(self) -> str:
        try:
            data = await self._fetch_runtime_json()
        except Exception as error:
            logger.error("Failed to fetch Hermes runtime JSON: %s", error)
            return f"同步失败：{error}"

        events = data.get("events", [])
        if not isinstance(events, list):
            return "同步失败：events 格式异常"

        changed = self.store.sync_deadlines(events)
        updated_at = data.get("updatedAt", "")
        msg = f"已同步 {len(events)} 个活动" + (f"，{changed} 项变更" if changed else "")
        if updated_at:
            msg += f"\n更新时间：{updated_at}"
        logger.info("Hermes sync: %d events, %d changed", len(events), changed)
        return msg

    # ── daily push ────────────────────────────────────────

    async def _daily_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self._maybe_send_daily()
                await self._maybe_send_start_reminders()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Deadline daily loop error: %s", error)
            await asyncio.sleep(60)

    def _tz(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return ZoneInfo(self._timezone())
        except ZoneInfoNotFoundError:
            return ZoneInfo("Asia/Shanghai")

    def _collect_items_for_target(self, target, now_iso: str) -> list[DeadlineItem]:
        items = self.store.list_active_deadlines(target_origin=target.target_origin, now_iso=now_iso)
        if target.target_kind == "group" and target.broadcast_enabled:
            items = self.store.list_active_deadlines(
                target_origin=self.store.BROADCAST_ORIGIN,
                now_iso=now_iso,
            ) + self.store.list_active_deadlines(
                target_origin=self.store.HERMES_ORIGIN,
                now_iso=now_iso,
            ) + items
        # Deduplicate by (title, category, due_at, starts_at)
        seen = set()
        deduped = []
        for item in items:
            key = (item.title, item.category, item.due_at[:16], (item.starts_at or "")[:16])
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        return deduped

    async def _maybe_send_daily(self) -> None:
        tz = self._tz()
        now = datetime.now(tz)
        if (now.hour, now.minute) < (self._daily_hour(), self._daily_minute()):
            return

        today = now.date().isoformat()
        now_iso = now.isoformat(timespec="minutes")
        for target in self.store.list_targets_for_daily(now_iso=now_iso):
            if target.target_origin == self.store.BROADCAST_ORIGIN or target.target_kind == "broadcast":
                continue
            if target.last_daily_date == today:
                continue
            items = self._collect_items_for_target(target, now_iso)
            if not items:
                self.store.update_last_daily_date(target_origin=target.target_origin, date_value=today)
                continue
            text = self._format_daily(items, now)
            await self.context.send_message(
                target.target_origin,
                MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]),
            )
            self.store.update_last_daily_date(target_origin=target.target_origin, date_value=today)

    # ── start reminder (idempotent) ───────────────────────

    async def _maybe_send_start_reminders(self) -> None:
        if not self._notify_on_start():
            return
        tz = self._tz()
        now = datetime.now(tz)
        now_iso = now.isoformat(timespec="minutes")
        advance = timedelta(days=self._start_advance_days())
        for target in self.store.list_targets_for_daily(now_iso=now_iso):
            if target.target_origin == self.store.BROADCAST_ORIGIN or target.target_kind == "broadcast":
                continue
            pending: list[tuple[DeadlineItem, str]] = []
            for item in self._collect_items_for_target(target, now_iso):
                if not item.starts_at:
                    continue
                starts = self._try_parse_dt(item.starts_at, tz)
                due = self._try_parse_dt(item.due_at, tz)
                if starts is None or due is None:
                    continue
                # 到点窗口：[starts - advance, starts + 1天]；已结束或过早都跳过
                if now < starts - advance:
                    continue
                if now > starts + timedelta(days=1):
                    continue
                if due < now:
                    continue
                key = item.source_id or f"manual:{item.id}"
                if self.store.has_start_notice(
                    target_origin=target.target_origin,
                    deadline_key=key,
                    starts_at=item.starts_at,
                ):
                    continue
                pending.append((item, key))
            if not pending:
                continue
            text = self._format_start_reminder([item for item, _ in pending], now)
            await self.context.send_message(
                target.target_origin,
                MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]),
            )
            for item, key in pending:
                self.store.record_start_notice(
                    target_origin=target.target_origin,
                    deadline_key=key,
                    starts_at=item.starts_at,
                )

    # ── formatting ─────────────────────────────────────────

    @staticmethod
    def _try_parse_dt(value: str, tz) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)

    def _split_by_start(self, items: list[DeadlineItem], now: datetime) -> tuple[list[DeadlineItem], list[DeadlineItem]]:
        ongoing: list[DeadlineItem] = []
        upcoming: list[DeadlineItem] = []
        for item in items:
            starts = self._try_parse_dt(item.starts_at, now.tzinfo) if item.starts_at else None
            if starts is not None and starts > now:
                upcoming.append(item)
            else:
                ongoing.append(item)
        return ongoing, upcoming

    def _format_group_lines(self, items: list[DeadlineItem], now: datetime, *, show_start: bool) -> list[str]:
        grouped: dict[str, list[DeadlineItem]] = defaultdict(list)
        for item in items:
            grouped[item.category].append(item)

        lines: list[str] = []
        for category in sorted(grouped.keys()):
            lines.extend(["", f"{category}："])
            for item in grouped[category]:
                due_at = datetime.fromisoformat(item.due_at).astimezone(now.tzinfo)
                lines.append(f"{item.title}")
                if show_start and item.starts_at:
                    starts = self._try_parse_dt(item.starts_at, now.tzinfo)
                    if starts is not None:
                        lines.append(
                            f"开始：{starts.strftime('%Y-%m-%d %H:%M')}（{self._format_distance(starts - now)}后开始）"
                        )
                    lines.append(f"🔔 结束：{due_at.strftime('%Y-%m-%d %H:%M')}")
                else:
                    lines.append(f"截止：{due_at.strftime('%Y-%m-%d %H:%M')}（剩余{self._format_distance(due_at - now)}）")
                if item.source_url:
                    lines.append(f"原文：{item.source_url}")
                lines.append("")
        return lines

    def _format_daily(self, items: list[DeadlineItem], now: datetime) -> str:
        ongoing, upcoming = self._split_by_start(items, now)
        lines = ["日程提醒"]
        if ongoing:
            lines.extend(["", "【进行中】"])
            lines.extend(self._format_group_lines(ongoing, now, show_start=False))
        if upcoming:
            lines.extend(["", "【未开始】"])
            lines.extend(self._format_group_lines(upcoming, now, show_start=True))
        return "\n".join(lines).strip()

    def _format_start_reminder(self, items: list[DeadlineItem], now: datetime) -> str:
        lines = ["活动开始提醒"]
        for item in items:
            starts = self._try_parse_dt(item.starts_at, now.tzinfo)
            starts_text = starts.strftime('%Y-%m-%d %H:%M') if starts else item.starts_at
            lines.extend(
                [
                    "",
                    f"🎉 {item.title} 将于 {starts_text} 开启！",
                ]
            )
            if item.source_url:
                lines.append(f"原文：{item.source_url}")
        return "\n".join(lines).strip()

    def _format_list(self, items: list[DeadlineItem], now: datetime) -> str:
        if not items:
            return "当前还没有日程。"
        ongoing, upcoming = self._split_by_start(items, now)
        sections: list[str] = []
        for label, group in (("进行中", ongoing), ("未开始", upcoming)):
            if not group:
                continue
            blocks: list[str] = []
            for item in group:
                due_at = datetime.fromisoformat(item.due_at).astimezone(now.tzinfo)
                distance = self._format_distance(due_at - now)
                status = "（已暂停）" if not item.enabled else ""
                url_line = f"\n原文：{item.source_url}" if item.source_url else ""
                start_dt = self._try_parse_dt(item.starts_at, now.tzinfo) if item.starts_at else None
                is_upcoming = start_dt is not None and start_dt > now
                if is_upcoming:
                    lines = [
                        f"{item.category}：{item.title}{status}",
                        f"开始：{start_dt.strftime('%Y-%m-%d %H:%M')}",
                        f"🔔 结束：{due_at.strftime('%Y-%m-%d %H:%M')}{url_line}",
                    ]
                else:
                    start_line = ""
                    if start_dt is not None:
                        start_line = f"\n开始：{start_dt.strftime('%Y-%m-%d %H:%M')}"
                    lines = [
                        f"{item.category}：{item.title}{status}",
                        f"截止：{due_at.strftime('%Y-%m-%d %H:%M')}（剩余{distance}）{start_line}{url_line}",
                    ]
                blocks.append("\n".join(lines))
            sections.append(f"—— {label} ——\n" + "\n\n".join(blocks))
        return "当前日程\n" + "\n\n".join(sections)

    @staticmethod
    def _format_distance(delta: timedelta) -> str:
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            total_seconds = abs(total_seconds)
            prefix = "已结束 "
        else:
            prefix = ""
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        if days:
            return f"{prefix}{days}天{hours}小时{minutes}分钟"
        if hours:
            return f"{prefix}{hours}小时{minutes}分钟"
        return f"{prefix}{minutes}分钟"

    # ── commands ───────────────────────────────────────────

    async def _handle(self, event: AstrMessageEvent, commands: tuple[str, ...]):
        origin = _event_origin(event)
        if not origin:
            yield event.plain_result("当前会话来源无法记录，请稍后重试。")
            return

        remainder = _strip_command(event.message_str or "", commands)
        parts = remainder.split(maxsplit=1)
        sub = parts[0].strip().lower() if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""
        if sub.startswith("广播") and sub != "广播":
            action = sub.removeprefix("广播").strip()
            args = f"{action} {args}".strip()
            sub = "广播"

        if not sub or sub in {"help", "帮助", "菜单"}:
            yield event.plain_result(_help_text(self._daily_hour(), self._daily_minute()))
            return

        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            tz = ZoneInfo(self._timezone())
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("Asia/Shanghai")
        now = datetime.now(tz)
        now_iso = now.isoformat(timespec="minutes")

        # Redirect: add/delete/pause/resume → Hermes
        if sub in {"add", "添加", "新增", "delete", "del", "删除", "移除", "pause", "暂停", "resume", "恢复"}:
            yield event.plain_result("活动管理已交由 Hermes 统一处理。\n请在飞书告诉 Hermes 添加/修改/删除活动，Bot 会自动同步。")
            return

        if sub in {"sync", "同步"}:
            if not self._can_manage(event):
                yield event.plain_result("权限不足。")
                return
            msg = await self._sync_from_hermes()
            yield event.plain_result(msg)
            return

        if sub in {"broadcast", "广播"}:
            async for result in self._handle_broadcast(event, origin, args, now, now_iso):
                yield result
            return

        if sub in {"list", "列表"}:
            items = self.store.list_deadlines(target_origin=origin, include_disabled=True)
            yield event.plain_result(_clamp(self._format_list(items, now), self.max_output_chars))
            return

        if sub in {"today", "preview", "今日", "预览"}:
            items = self.store.list_active_deadlines(target_origin=origin, now_iso=now_iso)
            if not items:
                yield event.plain_result("当前没有未结束的日程。")
                return
            yield event.plain_result(_clamp(self._format_daily(items, now), self.max_output_chars))
            return

        if sub in {"status", "状态"}:
            target = self.store.get_target(origin)
            enabled = target.enabled if target else True
            last_date = target.last_daily_date if target else "-"
            yield event.plain_result(
                "\n".join(
                    [
                        "日程提醒状态",
                        f"当前会话推送：{'开启' if enabled else '关闭'}",
                        f"每日时间：{self._daily_hour():02d}:{self._daily_minute():02d}",
                        f"上次推送日期：{last_date or '-'}",
                        "数据源：Hermes 自动同步",
                    ]
                )
            )
            return

        if sub in {"on", "开", "开启"}:
            self.store.set_target_enabled(target_origin=origin, target_kind=_target_kind(event), enabled=True)
            yield event.plain_result("已开启当前会话的每日 DDL 推送。")
            return

        if sub in {"off", "关", "关闭"}:
            self.store.set_target_enabled(target_origin=origin, target_kind=_target_kind(event), enabled=False)
            yield event.plain_result("已关闭当前会话的每日 DDL 推送。")
            return

        yield event.plain_result(_help_text(self._daily_hour(), self._daily_minute()))

    async def _handle_broadcast(
        self,
        event: AstrMessageEvent,
        origin: str,
        args: str,
        now: datetime,
        now_iso: str,
    ):
        parts = str(args or "").strip().split(maxsplit=1)
        action = parts[0].strip().lower() if parts else ""
        value = parts[1].strip() if len(parts) > 1 else ""

        if action in {"join", "加入", "订阅"}:
            if not _is_group(event):
                yield event.plain_result("广播加入只能在群聊中执行。")
                return
            if not self._can_manage(event):
                yield event.plain_result("权限不足。")
                return
            self.store.set_broadcast_enabled(target_origin=origin, target_kind=_target_kind(event), enabled=True)
            yield event.plain_result("当前群已加入 DDL 广播。以后广播日程和 Hermes 活动会每天推送到这个群。")
            return

        if action in {"leave", "退出", "取消"}:
            if not _is_group(event):
                yield event.plain_result("广播退出只能在群聊中执行。")
                return
            if not self._can_manage(event):
                yield event.plain_result("权限不足。")
                return
            self.store.set_broadcast_enabled(target_origin=origin, target_kind=_target_kind(event), enabled=False)
            yield event.plain_result("当前群已退出 DDL 广播。")
            return

        if action in {"list", "列表", "status", "状态"}:
            items = self.store.list_deadlines(target_origin=origin, include_disabled=True)
            if items:
                yield event.plain_result(_clamp(self._format_list(items, now).replace("当前日程\n", "", 1), self.max_output_chars))
            else:
                yield event.plain_result("没有日程。")
            return

        if action in {"today", "preview", "今日", "预览"}:
            items = self.store.list_active_deadlines(target_origin=origin, now_iso=now_iso)
            if not items:
                yield event.plain_result("当前没有未结束的日程。")
                return
            yield event.plain_result(_clamp(self._format_daily(items, now), self.max_output_chars))
            return

        # Redirect broadcast add/delete/pause/resume
        if action in {"add", "添加", "新增", "delete", "del", "删除", "移除", "pause", "暂停", "resume", "恢复"}:
            yield event.plain_result("广播活动管理已交由 Hermes 统一处理。请在飞书告诉 Hermes。")
            return

        yield event.plain_result(_help_text(self._daily_hour(), self._daily_minute()))

    @filter.command("ddl")
    async def ddl(self, event: AstrMessageEvent):
        async for result in self._handle(event, ("ddl",)):
            yield result

    @filter.command("日程")
    async def schedule(self, event: AstrMessageEvent):
        async for result in self._handle(event, ("日程",)):
            yield result
