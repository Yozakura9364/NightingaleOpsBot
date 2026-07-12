from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .qr_client import SqmallQrClient
from .sqmall_engine import run_sqmall_session_sign, run_sqmall_sign
from .storage import DEFAULT_SLOT, SqmallCredentialStore


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
    sensitive_prefixes = (
        "DAOYU_KEY",
        "DAOYUKEY",
        "DY_KEY",
        "DaoyuKey",
        "SESSION_ID",
        "SESSIONID",
        "SHOW_USERNAME",
        "SHOWUSERNAME",
        "ShowUsername",
        "NICKNAME",
        "USER_ID",
        "USERID",
        "MEMBER_ID",
        "MEMBERID",
        "DISPLAY_NAME",
        "DISPLAYNAME",
        "叨鱼KEY",
        "叨鱼Key",
        "手机号",
        "昵称",
        "用户ID",
        "商城会话",
        "会员ID",
    )
    if first_line and not any(first_line.lower().startswith(prefix.lower()) for prefix in sensitive_prefixes):
        return first_line
    return ""


def _slot_from_command(text: str, commands: tuple[str, ...], *, allow_all: bool = False) -> str:
    value = _command_argument(text, commands)
    if allow_all and value == "全部":
        return value
    return SqmallCredentialStore.validate_slot(value)


def _rename_slots_from_command(text: str, commands: tuple[str, ...]) -> tuple[str, str]:
    value = _command_argument(text, commands)
    parts = value.split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("格式：/盛趣商城改名 旧槽位名 新槽位名")
    old_slot = SqmallCredentialStore.normalize_slot(parts[0])
    new_slot = SqmallCredentialStore.validate_slot(parts[1])
    return old_slot, new_slot


def _normalize_daoyu_key(value: str) -> str:
    raw = value.strip()
    if len(raw) < 10 or not raw.startswith("DY"):
        raise ValueError("DAOYU_KEY 格式不正确，应以 DY 开头。")
    return raw


def _normalize_show_username(value: str) -> str:
    raw = value.strip()
    if len(raw) < 5:
        raise ValueError("SHOW_USERNAME 看起来过短，请填写叨鱼显示用户名。")
    return raw


def _normalize_nickname(value: str) -> str:
    raw = value.strip()
    if len(raw) < 2:
        raise ValueError("NICKNAME 看起来过短，请填写网页请求里的 nickname。")
    return raw


def _normalize_user_id(value: str) -> str:
    raw = value.strip()
    if not raw.isdigit():
        raise ValueError("USER_ID 格式不正确，应为纯数字。")
    return raw


def _normalize_session_id(value: str) -> str:
    raw = value.strip()
    if len(raw) < 16:
        raise ValueError("SESSION_ID 看起来过短，请填写网页请求里的 sessionId。")
    return raw


def _normalize_member_id(value: str) -> str:
    raw = value.strip()
    if not raw.isdigit():
        raise ValueError("MEMBER_ID 格式不正确，应为纯数字。")
    return raw


def _has_manual_credentials(text: str) -> bool:
    return bool(
        _extract_field(text, ("SESSION_ID", "SESSIONID", "sessionId", "商城会话"))
        or _extract_field(text, ("MEMBER_ID", "MEMBERID", "memberId", "会员ID"))
        or _extract_field(text, ("DAOYU_KEY", "DAOYUKEY", "DY_KEY", "DaoyuKey", "叨鱼KEY", "叨鱼Key"))
        or _extract_field(text, ("SHOW_USERNAME", "SHOWUSERNAME", "ShowUsername", "showusername", "手机号"))
        or _extract_field(text, ("NICKNAME", "nickname", "昵称"))
        or _extract_field(text, ("USER_ID", "USERID", "user_id", "用户ID", "用户Id"))
    )


def _help_text() -> str:
    return "\n".join(
        [
            "盛趣积分商城自动签到",
            "",
            "私聊扫码绑定：",
            "/盛趣商城绑定 [槽位名]",
            "机器人会发送二维码，请用叨鱼或微信扫码并确认登录。",
            "扫码绑定会保存独立网页登录态，后续签到前会刷新商城 session。",
            "",
            "手工绑定兜底：",
            "/盛趣商城绑定 [槽位名]",
            "SESSION_ID: login-xxxxxxxx",
            "MEMBER_ID: 1795361933",
            "",
            "兼容旧格式：",
            "DAOYU_KEY: DY_...",
            "USER_ID: 807483",
            "NICKNAME: sdo807483",
            "或",
            "SHOW_USERNAME: 138****1234",
            "",
            "常用命令：",
            "/盛趣商城状态",
            "/盛趣商城签到",
            "",
            "多账号命令：",
            "/盛趣商城绑定 小号1",
            "/盛趣商城状态 小号1",
            "/盛趣商城签到 小号1",
            "/盛趣商城改名 小号1 新名字",
            "/盛趣商城解绑 小号1",
            "",
            "不写槽位名时使用“默认”。“/盛趣商城签到”默认签到全部槽位。",
            "群里发送“/盛趣商城签到”只会提示私聊绑定，不会接收账号凭证。",
        ]
    )
def _slot_not_found_text(slot: str) -> str:
    if slot in {"名字", "槽位名", "[名字]", "[槽位名]"}:
        return (
            "这里的“名字”只是占位符，请换成绑定列表里的实际槽位名。\n"
            "例如：/盛趣商城状态 默认"
        )
    return f"你还没有绑定槽位“{slot}”。私聊发送“/盛趣商城绑定 {slot}”绑定。"


@register(
    "astrbot_plugin_sqmall_sign",
    "NightingaleSilence",
    "盛趣积分商城私聊绑定与自动签到插件。",
    "0.1.0",
)
class SqmallSignPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_output_chars = int(self.config.get("max_output_chars", 1800) or 1800)
        self.data_dir = Path(__file__).resolve().parent / ".local"
        self.store = SqmallCredentialStore(self.data_dir)
        self.qr_client = SqmallQrClient(
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
            logger.info("SQMall auto sign loop started.")

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

    async def _send_private(self, private_origin: str, text: str) -> None:
        if not private_origin:
            return
        await self.context.send_message(private_origin, MessageChain([Comp.Plain(text)]))

    async def _refresh_browser_state_credential(
        self,
        user_id: str,
        slot: str,
        private_origin: str,
        credential,
    ) -> tuple[str, str, str]:
        if not self.qr_client.configured:
            raise RuntimeError("runner 未配置，无法刷新盛趣商城扫码登录态")

        response = await self.qr_client.refresh_browser_state(credential.secret)
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "runner 刷新盛趣商城登录态失败")

        session = response.get("session") or {}
        if session.get("status") != "success" or not session.get("loggedIn"):
            result_msg = ((session.get("loginState") or {}).get("resultMsg") or "").strip()
            raise RuntimeError(result_msg or "扫码登录态已失效，请重新扫码绑定")

        refreshed = session.get("credential") or {}
        browser_state = str(refreshed.get("browserState") or "").strip()
        session_id = str(refreshed.get("sessionId") or "").strip()
        member_id = str(refreshed.get("memberId") or credential.member_id or "").strip()
        display_name = str(refreshed.get("displayName") or credential.show_username or member_id).strip()
        if not browser_state or not session_id or not member_id:
            raise RuntimeError("runner 已登录，但没有返回完整的商城会话凭证")

        self.store.bind_browser_state(
            user_id,
            private_origin,
            browser_state,
            member_id,
            display_name,
            slot=slot,
        )
        return session_id, member_id, display_name

    async def _run_for_user(
        self,
        user_id: str,
        *,
        slot: str = DEFAULT_SLOT,
        auto_date: str | None = None,
    ) -> tuple[bool, str]:
        account = self.store.get_account(user_id, slot)
        if not account:
            return False, _slot_not_found_text(slot)
        credential = self.store.get_credential(user_id, slot)
        if not credential:
            return False, _slot_not_found_text(slot)

        try:
            if credential.kind == "sqmall-browser-state":
                session_id, member_id, display_name = await self._refresh_browser_state_credential(
                    user_id,
                    slot,
                    account.private_origin,
                    credential,
                )
                result = await asyncio.to_thread(
                    run_sqmall_session_sign,
                    session_id,
                    member_id,
                    display_name,
                )
            elif credential.kind == "sqmall-session":
                result = await asyncio.to_thread(
                    run_sqmall_session_sign,
                    credential.secret,
                    credential.member_id,
                    credential.show_username,
                )
            else:
                result = await asyncio.to_thread(
                    run_sqmall_sign,
                    credential.secret,
                    credential.show_username,
                    credential.member_id,
                )
            message = result.summary
            self.store.update_result(user_id, slot=slot, ok=result.ok, message=message, auto_date=auto_date)
            return result.ok, message
        except Exception as error:
            message = f"盛趣商城签到失败：{error}"
            self.store.update_result(user_id, slot=slot, ok=False, message=message, auto_date=auto_date)
            logger.warning("SQMall sign failed for %s/%s: %s", user_id, slot, error)
            return False, message

    async def _auto_sign_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self._maybe_run_auto_sign()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("SQMall auto sign loop error: %s", error)
            await asyncio.sleep(300)

    async def _maybe_run_auto_sign(self) -> None:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour = int(self.config.get("auto_sign_hour", 9) or 9)
        minute = int(self.config.get("auto_sign_minute", 40) or 40)
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
                prefix = "盛趣商城自动签到成功" if ok else "盛趣商城自动签到失败"
                slot_line = "" if account.slot == DEFAULT_SLOT else f"槽位：{account.slot}\n"
                await self._send_private(
                    account.private_origin,
                    _clamp(f"{prefix}\n{slot_line}\n{message}", self.max_output_chars),
                )

    async def _handle_bind(self, event: AstrMessageEvent):
        if not _is_private(event):
            yield event.plain_result("为了保护账号凭证，请私聊我发送“/盛趣商城绑定”。不要在群里发 DAOYU_KEY。")
            return

        text = event.message_str or ""
        if not _has_manual_credentials(text):
            async for result in self._handle_qr_bind(
                event,
                commands=("盛趣商城绑定", "盛趣绑定", "商城绑定"),
            ):
                yield result
            return

        try:
            slot = _slot_from_command(text, ("盛趣商城绑定", "盛趣绑定", "商城绑定"))
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        daoyu_key = _extract_field(text, ("DAOYU_KEY", "DAOYUKEY", "DY_KEY", "DaoyuKey", "叨鱼KEY", "叨鱼Key"))
        session_secret = _extract_field(text, ("SESSION_ID", "SESSIONID", "sessionId", "商城会话"))
        member_id = _extract_field(text, ("MEMBER_ID", "MEMBERID", "memberId", "会员ID"))
        display_name = _extract_field(text, ("DISPLAY_NAME", "DISPLAYNAME", "displayName", "昵称", "账号"))
        show_username = _extract_field(
            text,
            ("SHOW_USERNAME", "SHOWUSERNAME", "ShowUsername", "showusername", "手机号"),
        )
        nickname = _extract_field(text, ("NICKNAME", "nickname", "昵称"))
        user_id = _extract_field(text, ("USER_ID", "USERID", "user_id", "用户ID", "用户Id"))
        if session_secret or member_id:
            if not session_secret or not member_id:
                yield event.plain_result("使用网页会话绑定时，请同时填写 SESSION_ID 和 MEMBER_ID。")
                return
            try:
                normalized_session_id = _normalize_session_id(session_secret)
                normalized_member_id = _normalize_member_id(member_id)
            except ValueError as error:
                yield event.plain_result(str(error))
                return

            origin = _private_origin(event)
            if not origin:
                yield event.plain_result("当前私聊来源无法记录，请稍后重试。")
                return

            normalized_display_name = str(display_name or "").strip() or "盛趣商城账号"
            self.store.bind_session(
                str(event.get_sender_id()),
                origin,
                normalized_session_id,
                normalized_member_id,
                normalized_display_name,
                slot=slot,
            )
            yield event.plain_result(
                "\n".join(
                    [
                        "盛趣商城绑定成功。",
                        f"槽位：{slot}",
                        f"MEMBER_ID：{normalized_member_id}",
                        "已保存网页 session，会话过期后需要重新获取。",
                    ]
                )
            )
            return

        if not daoyu_key or not (show_username or nickname or user_id):
            yield event.plain_result(_help_text())
            return

        try:
            normalized_daoyu_key = _normalize_daoyu_key(daoyu_key)
            if nickname or user_id:
                if not nickname or not user_id:
                    raise ValueError("使用网页请求绑定时，请同时填写 USER_ID 和 NICKNAME。")
                normalized_identity = _normalize_nickname(nickname)
                normalized_user_id = _normalize_user_id(user_id)
                identity_label = "NICKNAME"
            else:
                normalized_identity = _normalize_show_username(show_username)
                normalized_user_id = ""
                identity_label = "SHOW_USERNAME"
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
            normalized_daoyu_key,
            normalized_identity,
            member_id=normalized_user_id,
            slot=slot,
        )
        yield event.plain_result(
            "\n".join(
                [
                    "盛趣商城绑定成功。",
                    f"槽位：{slot}",
                    f"{identity_label}：{normalized_identity}",
                    "之后可以私聊发送“/盛趣商城签到”，自动签到也会每日执行。",
                ]
            )
        )

    async def _handle_qr_bind(self, event: AstrMessageEvent, *, commands: tuple[str, ...]):
        if not _is_private(event):
            yield event.plain_result("为了保护账号信息，请私聊我发送“/盛趣商城绑定”。")
            return
        if not self.qr_client.configured:
            yield event.plain_result("盛趣商城扫码绑定暂未配置 runner 连接。可以发送“/盛趣商城帮助”查看手工绑定兜底格式。")
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

                qr_path = self.data_dir / "qr" / f"sqmall-{session_id}.png"
                await self.qr_client.download_image(session_id, qr_path)
                yield event.chain_result(
                    [
                        Comp.Plain(
                            "请使用微信扫描二维码并在手机端确认登录。"
                            f"\n二维码约 {timeout_seconds} 秒内有效。"
                            "\n这个二维码不能用叨鱼扫，叨鱼会把它当普通链接打开。"
                            "\n请只在私聊中操作，不要转发二维码。"
                        ),
                        Comp.Image(file=str(qr_path)),
                    ]
                )

                deadline = asyncio.get_running_loop().time() + timeout_seconds
                last_status_session = {}
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(3)
                    status_response = await self.qr_client.status(session_id)
                    if not status_response.get("ok"):
                        error = status_response.get("error") or "未知错误"
                        yield event.plain_result(f"查询扫码状态失败：{error}")
                        return

                    status_session = status_response.get("session") or {}
                    last_status_session = status_session
                    if not status_session.get("loggedIn"):
                        continue

                    credential = status_session.get("credential") or {}
                    credential_kind = str(credential.get("kind") or "").strip()
                    if credential_kind == "sqmall-browser-state":
                        browser_state = str(credential.get("browserState") or "").strip()
                        session_secret = str(credential.get("sessionId") or "").strip()
                        member_id = str(credential.get("memberId") or "").strip()
                        display_name = str(credential.get("displayName") or "").strip() or "盛趣商城账号"
                        if not browser_state or not session_secret or not member_id:
                            yield event.plain_result("扫码已登录，但没有拿到完整的商城登录态，请稍后重试。")
                            return

                        self.store.bind_browser_state(
                            str(event.get_sender_id()),
                            origin,
                            browser_state,
                            member_id,
                            display_name,
                            slot=slot,
                        )
                        yield event.plain_result(
                            "\n".join(
                                [
                                    "盛趣商城扫码绑定成功。",
                                    f"槽位：{slot}",
                                    f"账号：{display_name}",
                                    "已保存独立扫码登录态，后续签到会先刷新商城 session。",
                                ]
                            )
                        )
                        return

                    if credential_kind == "daoyu":
                        daoyu_key = _normalize_daoyu_key(str(credential.get("daoyuKey") or ""))
                        show_username = _normalize_show_username(str(credential.get("showUsername") or ""))
                        self.store.bind(
                            str(event.get_sender_id()),
                            origin,
                            daoyu_key,
                            show_username,
                            slot=slot,
                        )
                        yield event.plain_result(
                            "\n".join(
                                [
                                    "盛趣商城扫码绑定成功。",
                                    f"槽位：{slot}",
                                    f"账号：{show_username}",
                                    "已保存叨鱼凭据。",
                                ]
                            )
                        )
                        return

                    session_secret = str(credential.get("sessionId") or "").strip()
                    member_id = str(credential.get("memberId") or "").strip()
                    display_name = str(credential.get("displayName") or "").strip() or "盛趣商城账号"
                    if not session_secret or not member_id:
                        yield event.plain_result("扫码已登录，但没有拿到商城签到所需凭证，请稍后重试。")
                        return

                    self.store.bind_session(
                        str(event.get_sender_id()),
                        origin,
                        session_secret,
                        member_id,
                        display_name,
                        slot=slot,
                    )
                    yield event.plain_result(
                        "\n".join(
                            [
                                "盛趣商城扫码绑定成功。",
                                f"槽位：{slot}",
                                f"账号：{display_name}",
                                "当前只拿到短期商城 session，过期后需要重新扫码。",
                            ]
                        )
                    )
                    return
                ticket_captured = bool(last_status_session.get("ticketCaptured"))
                mall_attempted = bool(last_status_session.get("mallSessionAttempted"))
                if not ticket_captured:
                    detail = "没有捕获到登录 ticket。请确认手机端不是只打开了二维码网址，而是出现并完成了登录确认；叨鱼不行就用微信扫。"
                elif not mall_attempted:
                    detail = "已捕获登录 ticket，但还没有开始落商城登录态。"
                else:
                    detail = "已捕获登录 ticket，但商城登录态仍未生效。"
                yield event.plain_result(
                    "扫码绑定超时。\n"
                    f"诊断：{detail}\n"
                    "请重新发送“/盛趣商城绑定”获取新的二维码。"
                )
            except Exception as error:
                logger.warning("SQMall QR bind failed for %s: %s", event.get_sender_id(), error)
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
            yield event.plain_result("为了保护账号信息，请私聊我发送“/盛趣商城解绑”。")
            return
        argument = _command_argument(event.message_str or "", ("盛趣商城解绑", "盛趣解绑", "商城解绑"))
        if not argument:
            yield event.plain_result("为了避免误删，请明确要解绑的槽位。例如：/盛趣商城解绑 默认")
            return
        try:
            slot = _slot_from_command(event.message_str or "", ("盛趣商城解绑", "盛趣解绑", "商城解绑"))
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
            yield event.plain_result("为了保护账号信息，请私聊我发送“/盛趣商城改名”。")
            return
        try:
            old_slot, new_slot = _rename_slots_from_command(
                event.message_str or "",
                ("盛趣商城改名", "盛趣改名", "商城改名"),
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

    @filter.command("盛趣商城帮助")
    async def sqmall_help(self, event: AstrMessageEvent):
        if not self._group_allowed(event):
            return
        yield event.plain_result(_help_text())

    @filter.command("盛趣商城绑定")
    async def bind_sqmall(self, event: AstrMessageEvent):
        async for result in self._handle_bind(event):
            yield result

    @filter.command("盛趣绑定")
    async def bind_sqmall_short(self, event: AstrMessageEvent):
        async for result in self._handle_bind(event):
            yield result

    @filter.command("商城绑定")
    async def bind_sqmall_alias(self, event: AstrMessageEvent):
        async for result in self._handle_bind(event):
            yield result

    @filter.command("盛趣商城状态")
    async def sqmall_status(self, event: AstrMessageEvent):
        if not self._group_allowed(event):
            return
        if not _is_private(event):
            yield event.plain_result("为了保护账号信息，请私聊我发送“/盛趣商城状态”。")
            return

        try:
            slot = _slot_from_command(event.message_str or "", ("盛趣商城状态", "盛趣状态", "商城状态"))
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        user_id = str(event.get_sender_id())
        if slot == DEFAULT_SLOT and not _command_argument(
            event.message_str or "",
            ("盛趣商城状态", "盛趣状态", "商城状态"),
        ):
            accounts = self.store.list_user_accounts(user_id)
            if not accounts:
                yield event.plain_result("你还没有绑定盛趣商城。私聊发送“/盛趣商城绑定”查看格式。")
                return
            lines = ["盛趣商城绑定列表："]
            for account in accounts:
                status = "未运行" if account.last_ok is None else ("成功" if account.last_ok else "失败")
                lines.append(f"- {account.slot}：{account.show_username}")
                lines.append(f"  上次运行：{account.last_run_at or '-'}，上次结果：{status}")
            lines.append("")
            lines.append("签到全部：/盛趣商城签到")
            lines.append("只签单个：/盛趣商城签到 槽位名")
            yield event.plain_result(_clamp("\n".join(lines), self.max_output_chars))
            return

        account = self.store.get_account(user_id, slot)
        if not account:
            yield event.plain_result(_slot_not_found_text(slot))
            return

        status = "未运行" if account.last_ok is None else ("成功" if account.last_ok else "失败")
        bind_method = (
            "扫码保活"
            if account.credential_kind == "sqmall-browser-state"
            else ("扫码 session" if account.credential_kind == "sqmall-session" else "叨鱼 KEY")
        )
        lines = [
            "盛趣商城绑定状态：已绑定",
            f"槽位：{account.slot}",
            f"账号：{account.show_username}",
            f"绑定方式：{bind_method}",
            f"更新时间：{account.updated_at}",
            f"上次运行：{account.last_run_at or '-'}",
            f"上次结果：{status}",
        ]
        if account.last_message:
            lines.extend(["", _clamp(account.last_message, 600)])
        yield event.plain_result(_clamp("\n".join(lines), self.max_output_chars))
    @filter.command("盛趣状态")
    async def sqmall_status_short(self, event: AstrMessageEvent):
        async for result in self.sqmall_status(event):
            yield result

    @filter.command("商城状态")
    async def sqmall_status_alias(self, event: AstrMessageEvent):
        async for result in self.sqmall_status(event):
            yield result

    @filter.command("盛趣商城解绑")
    async def unbind_sqmall(self, event: AstrMessageEvent):
        async for result in self._handle_unbind(event):
            yield result

    @filter.command("盛趣解绑")
    async def unbind_sqmall_short(self, event: AstrMessageEvent):
        async for result in self._handle_unbind(event):
            yield result

    @filter.command("商城解绑")
    async def unbind_sqmall_alias(self, event: AstrMessageEvent):
        async for result in self._handle_unbind(event):
            yield result

    @filter.command("盛趣商城改名")
    async def rename_sqmall(self, event: AstrMessageEvent):
        async for result in self._handle_rename(event):
            yield result

    @filter.command("盛趣改名")
    async def rename_sqmall_short(self, event: AstrMessageEvent):
        async for result in self._handle_rename(event):
            yield result

    @filter.command("商城改名")
    async def rename_sqmall_alias(self, event: AstrMessageEvent):
        async for result in self._handle_rename(event):
            yield result

    @filter.command("盛趣商城签到")
    async def sqmall_sign(self, event: AstrMessageEvent):
        if not self._group_allowed(event):
            return
        if not _is_private(event):
            yield event.plain_result("为避免泄露账号信息，请私聊我发送“/盛趣商城签到”。")
            return

        argument = _command_argument(event.message_str or "", ("盛趣商城签到", "盛趣签到", "商城签到"))
        try:
            slot = _slot_from_command(
                event.message_str or "",
                ("盛趣商城签到", "盛趣签到", "商城签到"),
                allow_all=True,
            ) if argument else "全部"
        except ValueError as error:
            yield event.plain_result(str(error))
            return

        async with self._run_lock:
            user_id = str(event.get_sender_id())
            if slot == "全部":
                accounts = self.store.list_user_accounts(user_id)
                if not accounts:
                    yield event.plain_result("你还没有绑定盛趣商城。私聊发送“/盛趣商城绑定”查看格式。")
                    return
                results = []
                for account in accounts:
                    ok, message = await self._run_for_user(user_id, slot=account.slot)
                    prefix = "成功" if ok else "失败"
                    results.append(f"【{account.slot}】{prefix}\n{message}")
                yield event.plain_result(_clamp("盛趣商城全部签到完成\n\n" + "\n\n".join(results), self.max_output_chars))
                return

            ok, message = await self._run_for_user(user_id, slot=slot)
        prefix = "盛趣商城签到成功" if ok else "盛趣商城签到失败"
        slot_line = "" if slot == DEFAULT_SLOT else f"槽位：{slot}\n"
        yield event.plain_result(_clamp(f"{prefix}\n{slot_line}\n{message}", self.max_output_chars))

    @filter.command("盛趣签到")
    async def sqmall_sign_short(self, event: AstrMessageEvent):
        async for result in self.sqmall_sign(event):
            yield result

    @filter.command("商城签到")
    async def sqmall_sign_alias(self, event: AstrMessageEvent):
        async for result in self.sqmall_sign(event):
            yield result
