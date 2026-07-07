from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import re
from contextlib import suppress
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType

from .sign_engine import (
    RisingstoneSignError,
    SignOptions,
    get_risingstone_account_summary,
    run_risingstone_house_check,
    run_risingstone_sign,
)
from .qr_client import RisingstoneQrClient
from .storage import DEFAULT_SLOT, CredentialStore

_RAW_COMMAND_PREFIXES = (
    "绑定石之家",
    "石之家绑定",
    "石之家扫码绑定",
    "扫码绑定石之家",
    "石之家帮助",
    "石之家状态",
    "解绑石之家",
    "石之家解绑",
    "石之家改名",
    "改名石之家",
    "石之家签到",
    "石之家房屋",
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


def _is_private(event: AstrMessageEvent) -> bool:
    return not _event_group_id(event)


def _private_origin(event: AstrMessageEvent) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "")


def _extract_field(text: str, names: tuple[str, ...]) -> str:
    pattern = r"(?im)^\s*(?:" + "|".join(re.escape(name) for name in names) + r")\s*[:：]\s*(.+?)\s*$"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


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
    if first_line and not re.match(r"(?i)^(COOKIE|USER_AGENT|User-Agent|USER-AGENT|UA)\s*[:：]", first_line):
        return first_line
    return ""


def _slot_from_command(text: str, commands: tuple[str, ...], *, allow_all: bool = False) -> str:
    value = _command_argument(text, commands)
    if allow_all and value == "全部":
        return value
    return CredentialStore.validate_slot(value)


def _rename_slots_from_command(text: str, commands: tuple[str, ...]) -> tuple[str, str]:
    value = _command_argument(text, commands)
    parts = value.split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("格式：/石之家改名 旧槽位名 新槽位名")
    old_slot = CredentialStore.normalize_slot(parts[0])
    new_slot = CredentialStore.validate_slot(parts[1])
    return old_slot, new_slot


def _normalize_cookie(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("缺少 COOKIE。")
    parts = [part.strip() for part in raw.split(";")]
    risingstone = next((part for part in parts if part.lower().startswith("ff14risingstones=")), "")
    if not risingstone:
        raise ValueError("COOKIE 必须包含 ff14risingstones=...。")
    return risingstone


def _normalize_user_agent(value: str) -> str:
    raw = value.strip()
    if len(raw) < 20:
        raise ValueError("USER_AGENT 看起来过短，请填写登录石之家时使用的完整 User-Agent。")
    return raw


def _help_text() -> str:
    return "\n".join(
        [
            "石之家自动签到",
            "",
            "私聊绑定：",
            "/石之家绑定 [槽位名]",
            "",
            "常用命令：",
            "/石之家状态",
            "/石之家签到",
            "/石之家房屋",
            "",
            "多账号命令：",
            "/石之家绑定 小号1",
            "/石之家状态 小号1",
            "/石之家签到 小号1",
            "/石之家改名 小号1 新名字",
            "/石之家解绑 小号1",
            "",
            "高级手动绑定：",
            "/绑定石之家 [槽位名]",
            "COOKIE: ff14risingstones=...",
            "USER_AGENT: Mozilla/5.0 ...",
            "",
            "不写槽位名时使用“默认”。“/石之家签到”默认签到全部槽位。",
            "群里发送“/石之家签到”只会提示私聊绑定，不会接收 COOKIE。",
        ]
    )


def _slot_not_found_text(slot: str) -> str:
    if slot in {"名字", "槽位名", "[名字]", "[槽位名]"}:
        return (
            "这里的“名字”只是占位符，请换成绑定列表里的实际槽位名。\n"
            "例如：/石之家状态 默认"
        )
    return f"你还没有绑定槽位“{slot}”。私聊发送“/石之家绑定 {slot}”绑定。"


def _has_manual_credentials(text: str) -> bool:
    return bool(
        _extract_field(text, ("COOKIE", "Cookie", "cookie"))
        or _extract_field(text, ("USER_AGENT", "User-Agent", "USER-AGENT", "UA", "ua"))
    )


def _raw_risingstone_command(text: str) -> str:
    first_line = str(text or "").strip().splitlines()[0].strip() if str(text or "").strip() else ""
    if first_line.startswith("/"):
        return ""
    for command in _RAW_COMMAND_PREFIXES:
        if first_line == command or first_line.startswith(command + " "):
            return command
    return ""


@register(
    "astrbot_plugin_risingstone_sign",
    "NightingaleSilence",
    "石之家私聊绑定与自动签到插件。",
    "0.1.0",
)
class RisingstoneSignPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_output_chars = int(self.config.get("max_output_chars", 1800) or 1800)
        self.data_dir = Path(__file__).resolve().parent / ".local"
        self.store = CredentialStore(self.data_dir)
        self.qr_client = RisingstoneQrClient(
            str(self.config.get("ops_endpoint", "http://host.docker.internal:18766") or ""),
            str(self.config.get("ops_access_token", "") or ""),
            timeout_seconds=int(self.config.get("qr_bind_timeout_seconds", 180) or 180) + 60,
        )
        self._auto_task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()
        self._qr_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.config.get("auto_sign_enabled", True):
            self._auto_task = asyncio.create_task(self._auto_sign_loop())
            logger.info("Risingstone auto sign loop started.")

    async def terminate(self) -> None:
        if self._auto_task:
            self._auto_task.cancel()
            try:
                await self._auto_task
            except asyncio.CancelledError:
                pass

    def _group_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = _event_group_id(event)
        if not group_id:
            return True
        allowed = _split_ids(self.config.get("allowed_group_ids", ""))
        return not allowed or group_id in allowed

    def _sign_options(self) -> SignOptions:
        return SignOptions(
            get_sign_reward=bool(self.config.get("get_sign_reward", True)),
            check_house_remain=bool(self.config.get("check_house_remain", False)),
            report_account_summary=bool(self.config.get("show_account_summary", True)),
        )

    def _show_account_summary(self) -> bool:
        return bool(self.config.get("show_account_summary", True))

    async def _send_private(self, private_origin: str, text: str) -> None:
        if not private_origin:
            return
        await self.context.send_message(private_origin, MessageChain([Comp.Plain(text)]))

    async def _get_account_summary(self, cookie: str, user_agent: str) -> str:
        if not self._show_account_summary():
            return ""
        try:
            return await asyncio.to_thread(get_risingstone_account_summary, cookie, user_agent)
        except Exception as error:
            logger.warning("Risingstone account summary failed: %s", error)
            return ""

    async def _run_for_user(
        self,
        user_id: str,
        *,
        slot: str = DEFAULT_SLOT,
        auto_date: str | None = None,
    ) -> tuple[bool, str]:
        credentials = self.store.get_credentials(user_id, slot)
        if not credentials:
            return False, _slot_not_found_text(slot)

        cookie, user_agent = credentials
        try:
            result = await asyncio.to_thread(
                run_risingstone_sign,
                cookie,
                user_agent,
                self._sign_options(),
            )
            message = result.summary
            self.store.update_result(user_id, slot=slot, ok=True, message=message, auto_date=auto_date)
            return True, message
        except Exception as error:
            message = f"石之家签到失败：{error}"
            self.store.update_result(user_id, slot=slot, ok=False, message=message, auto_date=auto_date)
            logger.warning("Risingstone sign failed for %s/%s: %s", user_id, slot, error)
            return False, message

    async def _run_house_for_user(
        self,
        user_id: str,
        *,
        slot: str = DEFAULT_SLOT,
    ) -> tuple[bool, str]:
        credentials = self.store.get_credentials(user_id, slot)
        if not credentials:
            return False, _slot_not_found_text(slot)

        cookie, user_agent = credentials
        try:
            result = await asyncio.to_thread(
                run_risingstone_house_check,
                cookie,
                user_agent,
                self._sign_options(),
            )
            return True, result.summary
        except Exception as error:
            message = f"石之家房屋检查失败：{error}"
            logger.warning("Risingstone house check failed for %s/%s: %s", user_id, slot, error)
            return False, message

    async def _auto_sign_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self._maybe_run_auto_sign()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Risingstone auto sign loop error: %s", error)
            await asyncio.sleep(300)

    async def _maybe_run_auto_sign(self) -> None:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour = int(self.config.get("auto_sign_hour", 9) or 9)
        minute = int(self.config.get("auto_sign_minute", 30) or 30)
        if (now.hour, now.minute) < (hour, minute):
            return

        today = now.date().isoformat()
        accounts = [account for account in self.store.list_accounts() if account.last_auto_date != today]
        if not accounts:
            return

        async with self._run_lock:
            for account in accounts:
                ok, message = await self._run_for_user(
                    account.user_id,
                    slot=account.slot,
                    auto_date=today,
                )
                prefix = "石之家自动签到成功" if ok else "石之家自动签到失败"
                slot_line = "" if account.slot == DEFAULT_SLOT else f"槽位：{account.slot}\n"
                await self._send_private(
                    account.private_origin,
                    _clamp(f"{prefix}\n{slot_line}\n{message}", self.max_output_chars),
                )

    async def _handle_bind(self, event: AstrMessageEvent):
        if not _is_private(event):
            yield event.plain_result("为了保护账号凭证，请私聊我发送“/石之家绑定”。不要在群里发 COOKIE。")
            return

        text = event.message_str or ""
        if not _has_manual_credentials(text):
            async for result in self._handle_qr_bind(event, commands=("绑定石之家", "石之家绑定")):
                yield result
            return

        try:
            slot = _slot_from_command(text, ("绑定石之家", "石之家绑定"))
        except ValueError as error:
            yield event.plain_result(str(error))
            return
        cookie = _extract_field(text, ("COOKIE", "Cookie", "cookie"))
        user_agent = _extract_field(text, ("USER_AGENT", "User-Agent", "USER-AGENT", "UA", "ua"))
        if not cookie or not user_agent:
            yield event.plain_result(_help_text())
            return

        try:
            normalized_cookie = _normalize_cookie(cookie)
            normalized_user_agent = _normalize_user_agent(user_agent)
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        origin = _private_origin(event)
        if not origin:
            yield event.plain_result("当前私聊来源无法记录，请稍后重试。")
            return

        self.store.bind(
            str(event.get_sender_id()),
            origin,
            normalized_cookie,
            normalized_user_agent,
            slot=slot,
        )
        account_summary = await self._get_account_summary(normalized_cookie, normalized_user_agent)
        lines = ["绑定成功。"]
        lines.append(f"槽位：{slot}")
        if account_summary:
            lines.append(f"绑定角色：{account_summary}")
        lines.append("之后可以私聊发送“/石之家签到”，自动签到也会每日执行。")
        yield event.plain_result("\n".join(lines))

    async def _handle_qr_bind(
        self,
        event: AstrMessageEvent,
        *,
        commands: tuple[str, ...] = ("石之家扫码绑定", "扫码绑定石之家"),
    ):
        if not _is_private(event):
            yield event.plain_result("为了保护账号信息，请私聊我发送“/石之家绑定”。")
            return
        if not self.qr_client.configured:
            yield event.plain_result("石之家扫码绑定暂未配置 runner 连接。可以使用“/绑定石之家”手动绑定。")
            return

        try:
            slot = _slot_from_command(event.message_str or "", commands)
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        origin = _private_origin(event)
        if not origin:
            yield event.plain_result("当前私聊来源无法记录，请稍后重试。")
            return

        timeout_seconds = int(self.config.get("qr_bind_timeout_seconds", 180) or 180)
        timeout_seconds = max(60, min(timeout_seconds, 300))
        session_id = ""
        qr_path: Path | None = None

        async with self._qr_lock:
            try:
                started = await self.qr_client.start_session(ttl_ms=timeout_seconds * 1000)
                if not started.get("ok"):
                    yield event.plain_result(f"创建二维码失败：{started.get('error', '未知错误')}")
                    return

                session = started.get("session") or {}
                session_id = str(session.get("sessionId") or "")
                if not session_id:
                    yield event.plain_result("创建二维码失败：runner 没有返回会话 ID。")
                    return

                qr_path = self.data_dir / "qr" / f"{session_id}.png"
                await self.qr_client.download_image(session_id, qr_path)
                yield event.chain_result(
                    [
                        Comp.Plain(
                            "请使用叨鱼或微信扫描二维码并在手机端确认登录。"
                            f"\n二维码约 {timeout_seconds} 秒内有效。"
                            "\n请只在私聊中操作，不要转发二维码。"
                        ),
                        Comp.Image(file=str(qr_path)),
                    ]
                )

                deadline = asyncio.get_running_loop().time() + timeout_seconds
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(3)
                    status_response = await self.qr_client.status(session_id)
                    if not status_response.get("ok"):
                        error = status_response.get("error") or "未知错误"
                        yield event.plain_result(f"查询扫码状态失败：{error}")
                        return

                    status_session = status_response.get("session") or {}
                    if not status_session.get("loggedIn"):
                        continue

                    credential = status_session.get("credential") or {}
                    cookie = _normalize_cookie(str(credential.get("cookie") or ""))
                    user_agent = _normalize_user_agent(str(credential.get("userAgent") or ""))
                    self.store.bind(
                        str(event.get_sender_id()),
                        origin,
                        cookie,
                        user_agent,
                        slot=slot,
                    )
                    account_summary = await self._get_account_summary(cookie, user_agent)
                    lines = ["扫码绑定成功。"]
                    lines.append(f"槽位：{slot}")
                    if account_summary:
                        lines.append(f"绑定角色：{account_summary}")
                    lines.append("之后可以私聊发送“/石之家签到”，自动签到也会每日执行。")
                    yield event.plain_result("\n".join(lines))
                    return

                yield event.plain_result("扫码绑定超时。请重新发送“/石之家绑定”获取新的二维码。")
            except Exception as error:
                logger.warning("Risingstone QR bind failed for %s: %s", event.get_sender_id(), error)
                yield event.plain_result(f"扫码绑定失败：{error}")
            finally:
                if session_id:
                    with suppress(Exception):
                        await self.qr_client.cancel(session_id)
                if qr_path:
                    with suppress(OSError):
                        qr_path.unlink()

    async def _handle_unbind(self, event: AstrMessageEvent):
        if not _is_private(event):
            yield event.plain_result("为了保护账号信息，请私聊我发送“/石之家解绑”。")
            return
        argument = _command_argument(event.message_str or "", ("解绑石之家", "石之家解绑"))
        if not argument:
            yield event.plain_result("为了避免误删，请明确要解绑的槽位。例如：/石之家解绑 默认")
            return
        try:
            slot = _slot_from_command(event.message_str or "", ("解绑石之家", "石之家解绑"))
        except ValueError as error:
            yield event.plain_result(str(error))
            return
        removed = self.store.unbind(str(event.get_sender_id()), slot)
        yield event.plain_result(
            f"已解绑“{slot}”并删除本地凭证。"
            if removed
            else _slot_not_found_text(slot)
        )

    async def _handle_rename(self, event: AstrMessageEvent):
        if not _is_private(event):
            yield event.plain_result("为了保护账号信息，请私聊我发送“/石之家改名”。")
            return
        try:
            old_slot, new_slot = _rename_slots_from_command(
                event.message_str or "",
                ("石之家改名", "改名石之家"),
            )
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        result = self.store.rename_slot(str(event.get_sender_id()), old_slot, new_slot)
        if result == "missing":
            yield event.plain_result(_slot_not_found_text(old_slot))
            return
        if result == "exists":
            yield event.plain_result(f"槽位“{new_slot}”已经存在，请换一个名字。")
            return
        if result == "same":
            yield event.plain_result(f"槽位名没有变化：{old_slot}")
            return
        yield event.plain_result(f"已改名：{old_slot} -> {new_slot}")

    @filter.event_message_type(EventMessageType.ALL)
    async def risingstone_raw_text_entry(self, event: AstrMessageEvent):
        command = _raw_risingstone_command(event.message_str or "")
        if not command or not self._group_allowed(event):
            return

        event.stop_event()

        if command in {"绑定石之家", "石之家绑定"}:
            async for result in self._handle_bind(event):
                yield result
            return
        if command in {"石之家扫码绑定", "扫码绑定石之家"}:
            async for result in self._handle_qr_bind(event):
                yield result
            return
        if command == "石之家帮助":
            yield event.plain_result(_help_text())
            return
        if command == "石之家状态":
            async for result in self.risingstone_status(event):
                yield result
            return
        if command in {"解绑石之家", "石之家解绑"}:
            async for result in self._handle_unbind(event):
                yield result
            return
        if command in {"石之家改名", "改名石之家"}:
            async for result in self._handle_rename(event):
                yield result
            return
        if command == "石之家签到":
            async for result in self.risingstone_sign(event):
                yield result
            return
        if command == "石之家房屋":
            async for result in self.risingstone_house(event):
                yield result
            return

    @filter.command("绑定石之家")
    async def bind_risingstone(self, event: AstrMessageEvent):
        async for result in self._handle_bind(event):
            yield result

    @filter.command("石之家绑定")
    async def bind_risingstone_alias(self, event: AstrMessageEvent):
        async for result in self._handle_bind(event):
            yield result

    @filter.command("石之家扫码绑定")
    async def bind_risingstone_qr(self, event: AstrMessageEvent):
        async for result in self._handle_qr_bind(event):
            yield result

    @filter.command("扫码绑定石之家")
    async def bind_risingstone_qr_alias(self, event: AstrMessageEvent):
        async for result in self._handle_qr_bind(event):
            yield result

    @filter.command("石之家帮助")
    async def risingstone_help(self, event: AstrMessageEvent):
        if not self._group_allowed(event):
            return
        yield event.plain_result(_help_text())

    @filter.command("石之家状态")
    async def risingstone_status(self, event: AstrMessageEvent):
        if not self._group_allowed(event):
            return
        if not _is_private(event):
            yield event.plain_result("为了保护账号信息，请私聊我发送“/石之家状态”。")
            return

        try:
            slot = _slot_from_command(event.message_str or "", ("石之家状态",))
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        user_id = str(event.get_sender_id())
        if slot == DEFAULT_SLOT and not _command_argument(event.message_str or "", ("石之家状态",)):
            accounts = self.store.list_user_accounts(user_id)
            if not accounts:
                yield event.plain_result("你还没有绑定石之家。私聊发送“/石之家绑定”开始扫码绑定。")
                return
            lines = ["石之家绑定列表："]
            for account in accounts:
                status = "未运行" if account.last_ok is None else ("成功" if account.last_ok else "失败")
                summary = ""
                if self._show_account_summary():
                    credentials = self.store.get_credentials(user_id, account.slot)
                    if credentials:
                        summary = await self._get_account_summary(*credentials)
                name = summary or "角色获取中/未获取到"
                lines.append(f"- {account.slot}：{name}")
                lines.append(f"  上次运行：{account.last_run_at or '-'}，上次结果：{status}")
            lines.append("")
            lines.append("签到全部：/石之家签到")
            lines.append("只签单个：/石之家签到 槽位名")
            yield event.plain_result(_clamp("\n".join(lines), self.max_output_chars))
            return

        account = self.store.get_account(user_id, slot)
        if not account:
            yield event.plain_result(_slot_not_found_text(slot))
            return

        status = "未运行" if account.last_ok is None else ("成功" if account.last_ok else "失败")
        lines = [
            "石之家绑定状态：已绑定",
            f"槽位：{account.slot}",
            f"更新时间：{account.updated_at}",
            f"上次运行：{account.last_run_at or '-'}",
            f"上次结果：{status}",
        ]
        if self._show_account_summary():
            credentials = self.store.get_credentials(user_id, slot)
            if credentials:
                account_summary = await self._get_account_summary(*credentials)
                if account_summary:
                    lines.insert(1, f"绑定角色：{account_summary}")
        if account.last_message:
            lines.extend(["", _clamp(account.last_message, 600)])
        yield event.plain_result(_clamp("\n".join(lines), self.max_output_chars))

    @filter.command("解绑石之家")
    async def unbind_risingstone(self, event: AstrMessageEvent):
        async for result in self._handle_unbind(event):
            yield result

    @filter.command("石之家解绑")
    async def unbind_risingstone_alias(self, event: AstrMessageEvent):
        async for result in self._handle_unbind(event):
            yield result

    @filter.command("石之家改名")
    async def rename_risingstone_slot(self, event: AstrMessageEvent):
        async for result in self._handle_rename(event):
            yield result

    @filter.command("改名石之家")
    async def rename_risingstone_slot_alias(self, event: AstrMessageEvent):
        async for result in self._handle_rename(event):
            yield result

    @filter.command("石之家签到")
    async def risingstone_sign(self, event: AstrMessageEvent):
        if not self._group_allowed(event):
            return
        if not _is_private(event):
            yield event.plain_result("为避免泄露账号信息，请私聊我发送“/石之家签到”。")
            return

        argument = _command_argument(event.message_str or "", ("石之家签到",))
        try:
            slot = _slot_from_command(event.message_str or "", ("石之家签到",), allow_all=True) if argument else "全部"
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        async with self._run_lock:
            user_id = str(event.get_sender_id())
            if slot == "全部":
                accounts = self.store.list_user_accounts(user_id)
                if not accounts:
                    yield event.plain_result("你还没有绑定石之家。私聊发送“/石之家绑定”开始扫码绑定。")
                    return
                results = []
                for account in accounts:
                    ok, message = await self._run_for_user(user_id, slot=account.slot)
                    prefix = "成功" if ok else "失败"
                    results.append(f"【{account.slot}】{prefix}\n{message}")
                yield event.plain_result(_clamp("石之家全部签到完成\n\n" + "\n\n".join(results), self.max_output_chars))
                return

            ok, message = await self._run_for_user(user_id, slot=slot)
        prefix = "石之家签到成功" if ok else "石之家签到失败"
        slot_line = "" if slot == DEFAULT_SLOT else f"槽位：{slot}\n"
        yield event.plain_result(_clamp(f"{prefix}\n{slot_line}\n{message}", self.max_output_chars))

    @filter.command("石之家房屋")
    async def risingstone_house(self, event: AstrMessageEvent):
        if not self._group_allowed(event):
            return
        if not _is_private(event):
            yield event.plain_result("为避免泄露账号信息，请私聊我发送“/石之家房屋”。")
            return

        argument = _command_argument(event.message_str or "", ("石之家房屋",))
        try:
            slot = _slot_from_command(event.message_str or "", ("石之家房屋",), allow_all=True) if argument else "全部"
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        async with self._run_lock:
            user_id = str(event.get_sender_id())
            if slot == "全部":
                accounts = self.store.list_user_accounts(user_id)
                if not accounts:
                    yield event.plain_result("你还没有绑定石之家。私聊发送“/石之家绑定”开始扫码绑定。")
                    return
                results = []
                for account in accounts:
                    ok, message = await self._run_house_for_user(user_id, slot=account.slot)
                    prefix = "成功" if ok else "失败"
                    results.append(f"【{account.slot}】{prefix}\n{message}")
                yield event.plain_result(_clamp("石之家房屋检查完成\n\n" + "\n\n".join(results), self.max_output_chars))
                return

            ok, message = await self._run_house_for_user(user_id, slot=slot)
        prefix = "石之家房屋检查成功" if ok else "石之家房屋检查失败"
        slot_line = "" if slot == DEFAULT_SLOT else f"槽位：{slot}\n"
        yield event.plain_result(_clamp(f"{prefix}\n{slot_line}\n{message}", self.max_output_chars))
