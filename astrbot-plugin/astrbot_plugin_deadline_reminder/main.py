from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .storage import DeadlineItem, DeadlineStore


TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
DATE_TIME_DASH_RE = re.compile(r"^(\d{4}-\d{1,2}-\d{1,2})-(\d{1,2}:\d{2})$")


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
            return first_line[len(command) :].strip()
    return first_line


def _help_text(hour: int, minute: int) -> str:
    return "\n".join(
        [
            "日程提醒",
            "",
            f"每天 {hour:02d}:{minute:02d} 推送当前会话未结束的 DDL。",
            "",
            "添加：/ddl 添加 国服活动 2026-08-01 23:59 活动名称",
            "添加：/ddl 添加 国际服活动 2026-08-01 活动名称",
            "也支持：/ddl 广播添加 国服活动 2026-7-13-22:59 活动名称",
            "查看：/ddl 列表",
            "预览：/ddl 今日",
            "删除：/ddl 删除 3",
            "暂停单条：/ddl 暂停 3",
            "恢复单条：/ddl 恢复 3",
            "关闭当前会话推送：/ddl 关",
            "开启当前会话推送：/ddl 开",
            "",
            "广播加入当前群：/ddl 广播加入",
            "添加广播日程：/ddl 广播添加 国服活动 2026-08-01 23:59 活动名称",
            "查看广播日程：/ddl 广播列表",
            "预览广播日程：/ddl 广播今日",
            "删除广播日程：/ddl 广播删除 3",
            "",
            "别名：/日程 帮助、/日程 添加 ...",
            "日期支持 YYYY-MM-DD、YYYY-MM-DD HH:MM、YYYY-M-D-HH:MM；只写日期时默认 23:59。",
        ]
    )


@register(
    "astrbot_plugin_deadline_reminder",
    "NightingaleSilence",
    "Daily deadline reminder for private chats and groups.",
    "0.1.0",
)
class DeadlineReminderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(__file__).resolve().parent / ".local"
        self.store = DeadlineStore(self.data_dir)
        self.max_output_chars = int(self.config.get("max_output_chars", 3000) or 3000)
        self._daily_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        if self.config.get("enabled", True):
            self._daily_task = asyncio.create_task(self._daily_loop())
            logger.info("Deadline reminder daily loop started.")

    async def terminate(self) -> None:
        if self._daily_task:
            self._daily_task.cancel()
            try:
                await self._daily_task
            except asyncio.CancelledError:
                pass

    def _timezone(self) -> ZoneInfo:
        name = str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            logger.warning("Invalid deadline reminder timezone %s, fallback to Asia/Shanghai.", name)
            return ZoneInfo("Asia/Shanghai")

    def _daily_hour(self) -> int:
        return max(0, min(23, int(self.config.get("daily_hour", 9) or 9)))

    def _daily_minute(self) -> int:
        return max(0, min(59, int(self.config.get("daily_minute", 0) or 0)))

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id())
        manager_ids = _split_ids(self.config.get("manager_user_ids", ""))
        if sender in manager_ids:
            return True
        if not self.config.get("manage_requires_admin", True):
            return True
        return bool(event.is_admin())

    async def _daily_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self._maybe_send_daily()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Deadline reminder daily loop error: %s", error)
            await asyncio.sleep(60)

    async def _maybe_send_daily(self) -> None:
        now = datetime.now(self._timezone())
        if (now.hour, now.minute) < (self._daily_hour(), self._daily_minute()):
            return

        today = now.date().isoformat()
        now_iso = now.isoformat(timespec="minutes")
        for target in self.store.list_targets_for_daily(now_iso=now_iso):
            if target.last_daily_date == today:
                continue
            items = self.store.list_active_deadlines(target_origin=target.target_origin, now_iso=now_iso)
            if target.target_kind == "group" and target.broadcast_enabled:
                items = self.store.list_active_deadlines(
                    target_origin=self.store.BROADCAST_ORIGIN,
                    now_iso=now_iso,
                ) + items
            if not items:
                self.store.update_last_daily_date(target_origin=target.target_origin, date_value=today)
                continue
            text = self._format_daily(items, now)
            await self.context.send_message(
                target.target_origin,
                MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]),
            )
            self.store.update_last_daily_date(target_origin=target.target_origin, date_value=today)

    def _parse_due_at(self, date_text: str, time_text: str = "") -> datetime:
        date_text = str(date_text or "").strip().replace("/", "-")
        time_text = str(time_text or "").strip()
        dash_match = DATE_TIME_DASH_RE.match(date_text)
        if dash_match and not time_text:
            date_text = dash_match.group(1)
            time_text = dash_match.group(2)
        if not re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", date_text):
            raise ValueError("日期格式不对，请使用 YYYY-MM-DD、YYYY-MM-DD HH:MM 或 YYYY-M-D-HH:MM。")
        if time_text:
            if not TIME_RE.match(time_text):
                raise ValueError("时间格式不对，请使用 HH:MM。")
            raw = f"{date_text} {time_text}"
        else:
            raw = f"{date_text} 23:59"
        value = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        return value.replace(tzinfo=self._timezone())

    def _parse_add_args(self, args: str) -> tuple[str, datetime, str]:
        parts = str(args or "").strip().split()
        if len(parts) < 3:
            raise ValueError("用法：/ddl 添加 国服活动 2026-08-01 23:59 活动名称")
        category = parts[0].strip()
        date_text = parts[1].strip()
        if len(parts) >= 4 and TIME_RE.match(parts[2].strip()):
            due_at = self._parse_due_at(date_text, parts[2].strip())
            title = " ".join(parts[3:]).strip()
        else:
            due_at = self._parse_due_at(date_text)
            title = " ".join(parts[2:]).strip()
        if not category:
            raise ValueError("分类不能为空。")
        if not title:
            raise ValueError("日程内容不能为空。")
        return category, due_at, title

    def _format_daily(self, items: list[DeadlineItem], now: datetime) -> str:
        grouped: dict[str, list[DeadlineItem]] = defaultdict(list)
        for item in items:
            grouped[item.category].append(item)

        lines = ["日程提醒"]
        for category in sorted(grouped.keys()):
            lines.extend(["", f"{category}："])
            for item in grouped[category]:
                due_at = datetime.fromisoformat(item.due_at).astimezone(self._timezone())
                lines.append(f"#{item.id} {item.title}")
                lines.append(f"结束时间：{due_at.strftime('%Y-%m-%d %H:%M')}")
                lines.append(f"距离结束还有 {self._format_distance(due_at - now)}")
                lines.append("")
        return "\n".join(lines).strip()

    def _format_list(self, items: list[DeadlineItem], now: datetime) -> str:
        if not items:
            return "当前会话还没有日程。"
        lines = ["当前会话日程"]
        for item in items:
            due_at = datetime.fromisoformat(item.due_at).astimezone(self._timezone())
            status = "开启" if item.enabled else "暂停"
            distance = self._format_distance(due_at - now)
            lines.append(
                f"#{item.id} [{status}] {item.category} / {item.title}\n"
                f"结束时间：{due_at.strftime('%Y-%m-%d %H:%M')}，距离结束还有 {distance}"
            )
        return "\n\n".join(lines)

    def _format_broadcast_status(self, now: datetime) -> str:
        items = self.store.list_deadlines(
            target_origin=self.store.BROADCAST_ORIGIN,
            include_disabled=True,
        )
        target_count = self.store.count_broadcast_targets()
        lines = [f"DDL 广播群数：{target_count}"]
        if items:
            lines.extend(["", self._format_list(items, now).replace("当前会话日程", "广播日程")])
        else:
            lines.append("广播日程为空。")
        return "\n".join(lines)

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

    async def _handle(self, event: AstrMessageEvent, commands: tuple[str, ...]):
        origin = _event_origin(event)
        if not origin:
            yield event.plain_result("当前会话来源无法记录，请稍后重试。")
            return

        target_kind = _target_kind(event)
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

        now = datetime.now(self._timezone())
        now_iso = now.isoformat(timespec="minutes")

        if sub in {"broadcast", "广播"}:
            async for result in self._handle_broadcast(event, origin, target_kind, args, now, now_iso):
                yield result
            return

        if sub in {"list", "列表"}:
            items = self.store.list_deadlines(target_origin=origin, include_disabled=True)
            yield event.plain_result(_clamp(self._format_list(items, now), self.max_output_chars))
            return

        if sub in {"today", "preview", "今日", "预览", "测试"}:
            items = self.store.list_active_deadlines(target_origin=origin, now_iso=now_iso)
            if not items:
                yield event.plain_result("当前会话没有未结束的日程。")
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
                    ]
                )
            )
            return

        if not self._can_manage(event):
            yield event.plain_result("权限不足：日程管理命令仅限管理员使用。")
            return

        if sub in {"add", "添加", "新增"}:
            try:
                category, due_at, title = self._parse_add_args(args)
            except ValueError as error:
                yield event.plain_result(str(error))
                return
            item = self.store.add_deadline(
                target_origin=origin,
                target_kind=target_kind,
                category=category,
                title=title,
                due_at=due_at.isoformat(timespec="minutes"),
                created_by=str(event.get_sender_id()),
            )
            yield event.plain_result(
                "\n".join(
                    [
                        f"已添加日程 #{item.id}",
                        f"分类：{item.category}",
                        f"内容：{item.title}",
                        f"结束时间：{due_at.strftime('%Y-%m-%d %H:%M')}",
                        f"距离结束还有 {self._format_distance(due_at - now)}",
                    ]
                )
            )
            return

        if sub in {"delete", "del", "删除", "移除"}:
            if not args.isdigit():
                yield event.plain_result("用法：/ddl 删除 3")
                return
            ok = self.store.delete_deadline(target_origin=origin, deadline_id=int(args))
            yield event.plain_result("已删除。" if ok else "没有找到这个日程。")
            return

        if sub in {"pause", "暂停"}:
            if not args.isdigit():
                yield event.plain_result("用法：/ddl 暂停 3")
                return
            ok = self.store.set_deadline_enabled(target_origin=origin, deadline_id=int(args), enabled=False)
            yield event.plain_result("已暂停。" if ok else "没有找到这个日程。")
            return

        if sub in {"resume", "恢复"}:
            if not args.isdigit():
                yield event.plain_result("用法：/ddl 恢复 3")
                return
            ok = self.store.set_deadline_enabled(target_origin=origin, deadline_id=int(args), enabled=True)
            yield event.plain_result("已恢复。" if ok else "没有找到这个日程。")
            return

        if sub in {"on", "开", "开启"}:
            self.store.set_target_enabled(target_origin=origin, target_kind=target_kind, enabled=True)
            yield event.plain_result("已开启当前会话的每日 DDL 推送。")
            return

        if sub in {"off", "关", "关闭"}:
            self.store.set_target_enabled(target_origin=origin, target_kind=target_kind, enabled=False)
            yield event.plain_result("已关闭当前会话的每日 DDL 推送；已有日程不会删除。")
            return

        yield event.plain_result(_help_text(self._daily_hour(), self._daily_minute()))

    async def _handle_broadcast(
        self,
        event: AstrMessageEvent,
        origin: str,
        target_kind: str,
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
                yield event.plain_result("权限不足：广播加入仅限管理员使用。")
                return
            self.store.set_broadcast_enabled(target_origin=origin, target_kind=target_kind, enabled=True)
            yield event.plain_result("当前群已加入 DDL 广播。以后广播日程会每天推送到这个群。")
            return

        if action in {"leave", "退出", "取消"}:
            if not _is_group(event):
                yield event.plain_result("广播退出只能在群聊中执行。")
                return
            if not self._can_manage(event):
                yield event.plain_result("权限不足：广播退出仅限管理员使用。")
                return
            self.store.set_broadcast_enabled(target_origin=origin, target_kind=target_kind, enabled=False)
            yield event.plain_result("当前群已退出 DDL 广播；群内自己的日程不受影响。")
            return

        if action in {"list", "列表", "状态", "status"}:
            yield event.plain_result(_clamp(self._format_broadcast_status(now), self.max_output_chars))
            return

        if action in {"today", "preview", "今日", "预览", "测试"}:
            items = self.store.list_active_deadlines(
                target_origin=self.store.BROADCAST_ORIGIN,
                now_iso=now_iso,
            )
            if not items:
                yield event.plain_result("当前没有未结束的广播日程。")
                return
            yield event.plain_result(_clamp(self._format_daily(items, now), self.max_output_chars))
            return

        if not self._can_manage(event):
            yield event.plain_result("权限不足：广播日程管理仅限管理员使用。")
            return

        if action in {"add", "添加", "新增"}:
            try:
                category, due_at, title = self._parse_add_args(value)
            except ValueError as error:
                yield event.plain_result(str(error).replace("/ddl 添加", "/ddl 广播添加"))
                return
            item = self.store.add_deadline(
                target_origin=self.store.BROADCAST_ORIGIN,
                target_kind="broadcast",
                category=category,
                title=title,
                due_at=due_at.isoformat(timespec="minutes"),
                created_by=str(event.get_sender_id()),
            )
            target_count = self.store.count_broadcast_targets()
            yield event.plain_result(
                "\n".join(
                    [
                        f"已添加广播日程 #{item.id}",
                        f"分类：{item.category}",
                        f"内容：{item.title}",
                        f"结束时间：{due_at.strftime('%Y-%m-%d %H:%M')}",
                        f"距离结束还有 {self._format_distance(due_at - now)}",
                        f"当前会推送到 {target_count} 个已加入广播的群。",
                    ]
                )
            )
            return

        if action in {"delete", "del", "删除", "移除"}:
            if not value.isdigit():
                yield event.plain_result("用法：/ddl 广播删除 3")
                return
            ok = self.store.delete_deadline(
                target_origin=self.store.BROADCAST_ORIGIN,
                deadline_id=int(value),
            )
            yield event.plain_result("已删除广播日程。" if ok else "没有找到这个广播日程。")
            return

        if action in {"pause", "暂停"}:
            if not value.isdigit():
                yield event.plain_result("用法：/ddl 广播暂停 3")
                return
            ok = self.store.set_deadline_enabled(
                target_origin=self.store.BROADCAST_ORIGIN,
                deadline_id=int(value),
                enabled=False,
            )
            yield event.plain_result("已暂停广播日程。" if ok else "没有找到这个广播日程。")
            return

        if action in {"resume", "恢复"}:
            if not value.isdigit():
                yield event.plain_result("用法：/ddl 广播恢复 3")
                return
            ok = self.store.set_deadline_enabled(
                target_origin=self.store.BROADCAST_ORIGIN,
                deadline_id=int(value),
                enabled=True,
            )
            yield event.plain_result("已恢复广播日程。" if ok else "没有找到这个广播日程。")
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
