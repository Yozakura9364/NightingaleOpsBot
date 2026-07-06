from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .sign_engine import RisingstoneSignError, SignOptions, run_risingstone_sign
from .storage import CredentialStore


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


def _is_private(event: AstrMessageEvent) -> bool:
    return event.get_group_id() is None


def _private_origin(event: AstrMessageEvent) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "")


def _extract_field(text: str, names: tuple[str, ...]) -> str:
    pattern = r"(?im)^\s*(?:" + "|".join(re.escape(name) for name in names) + r")\s*[:：]\s*(.+?)\s*$"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


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
            "绑定石之家",
            "COOKIE: ff14risingstones=...",
            "USER_AGENT: Mozilla/5.0 ...",
            "",
            "私聊命令：",
            "石之家签到",
            "石之家状态",
            "解绑石之家",
            "",
            "群里发送“石之家签到”只会提示私聊绑定，不会接收 COOKIE。",
        ]
    )


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
        self.store = CredentialStore(Path(__file__).resolve().parent / ".local")
        self._auto_task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()

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
        group_id = event.get_group_id()
        if group_id is None:
            return True
        allowed = _split_ids(self.config.get("allowed_group_ids", ""))
        return not allowed or str(group_id) in allowed

    def _sign_options(self) -> SignOptions:
        return SignOptions(
            get_sign_reward=bool(self.config.get("get_sign_reward", True)),
            check_house_remain=bool(self.config.get("check_house_remain", False)),
        )

    async def _send_private(self, private_origin: str, text: str) -> None:
        if not private_origin:
            return
        await self.context.send_message(private_origin, MessageChain([Comp.Plain(text)]))

    async def _run_for_user(self, user_id: str, *, auto_date: str | None = None) -> tuple[bool, str]:
        credentials = self.store.get_credentials(user_id)
        if not credentials:
            return False, "你还没有绑定石之家。私聊发送“绑定石之家”查看格式。"

        cookie, user_agent = credentials
        try:
            result = await asyncio.to_thread(
                run_risingstone_sign,
                cookie,
                user_agent,
                self._sign_options(),
            )
            message = result.summary
            self.store.update_result(user_id, ok=True, message=message, auto_date=auto_date)
            return True, message
        except Exception as error:
            message = f"石之家签到失败：{error}"
            self.store.update_result(user_id, ok=False, message=message, auto_date=auto_date)
            logger.warning("Risingstone sign failed for %s: %s", user_id, error)
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
                ok, message = await self._run_for_user(account.user_id, auto_date=today)
                prefix = "石之家自动签到成功" if ok else "石之家自动签到失败"
                await self._send_private(
                    account.private_origin,
                    _clamp(f"{prefix}\n\n{message}", self.max_output_chars),
                )

    async def _handle_bind(self, event: AstrMessageEvent):
        if not _is_private(event):
            yield event.plain_result("为了保护账号凭证，请私聊我发送“绑定石之家”。不要在群里发 COOKIE。")
            return

        text = event.message_str or ""
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
        )
        yield event.plain_result("绑定成功。之后可以私聊发送“石之家签到”手动签到，自动签到也会每日执行。")

    async def _handle_unbind(self, event: AstrMessageEvent):
        if not _is_private(event):
            yield event.plain_result("为了保护账号信息，请私聊我发送“解绑石之家”。")
            return
        removed = self.store.unbind(str(event.get_sender_id()))
        yield event.plain_result("已解绑并删除本地凭证。" if removed else "你还没有绑定石之家。")

    @filter.command("绑定石之家")
    async def bind_risingstone(self, event: AstrMessageEvent):
        async for result in self._handle_bind(event):
            yield result

    @filter.command("石之家绑定")
    async def bind_risingstone_alias(self, event: AstrMessageEvent):
        async for result in self._handle_bind(event):
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
            yield event.plain_result("为了保护账号信息，请私聊我发送“石之家状态”。")
            return

        account = self.store.get_account(str(event.get_sender_id()))
        if not account:
            yield event.plain_result("你还没有绑定石之家。私聊发送“绑定石之家”查看格式。")
            return

        status = "未运行" if account.last_ok is None else ("成功" if account.last_ok else "失败")
        lines = [
            "石之家绑定状态：已绑定",
            f"更新时间：{account.updated_at}",
            f"上次运行：{account.last_run_at or '-'}",
            f"上次结果：{status}",
        ]
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

    @filter.command("石之家签到")
    async def risingstone_sign(self, event: AstrMessageEvent):
        if not self._group_allowed(event):
            return
        if not _is_private(event):
            yield event.plain_result("为避免泄露账号信息，请私聊我发送“石之家签到”。")
            return

        async with self._run_lock:
            ok, message = await self._run_for_user(str(event.get_sender_id()))
        prefix = "石之家签到成功" if ok else "石之家签到失败"
        yield event.plain_result(_clamp(f"{prefix}\n\n{message}", self.max_output_chars))
