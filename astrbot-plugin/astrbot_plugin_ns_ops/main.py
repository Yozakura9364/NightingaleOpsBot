import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .formatters import format_health, format_help, format_job_response
from .ops_client import NsOpsClient


def _split_ids(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return {part.strip() for part in str(value).replace("\n", ",").split(",") if part.strip()}


@register(
    "astrbot_plugin_ns_ops",
    "NightingaleSilence",
    "通过 AstrBot 调用 NS Ops Runner 的受控服务器运维入口。",
    "0.1.0",
)
class NsOpsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_output_chars = int(self.config.get("max_output_chars", 3500) or 3500)
        self.client = NsOpsClient(
            endpoint=self.config.get("ops_endpoint", "http://host.docker.internal:18766"),
            token=self.config.get("access_token", ""),
        )
        self._traffic_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        if self.config.get("traffic_report_enabled", True):
            self._traffic_task = asyncio.create_task(self._traffic_report_loop())
            logger.info("NS Ops traffic report loop started.")

    async def terminate(self) -> None:
        if self._traffic_task:
            self._traffic_task.cancel()
            try:
                await self._traffic_task
            except asyncio.CancelledError:
                pass

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        if not event.is_admin():
            return False

        sender = str(event.get_sender_id())
        allowed_senders = _split_ids(self.config.get("allowed_sender_ids", ""))
        if allowed_senders and sender not in allowed_senders:
            return False

        group_id = event.get_group_id()
        allowed_groups = _split_ids(self.config.get("allowed_group_ids", ""))
        if allowed_groups and group_id is not None and str(group_id) not in allowed_groups:
            return False

        return True

    @staticmethod
    def _strip_ns_prefix(text: str) -> str:
        normalized = (text or "").strip()
        lowered = normalized.lower()
        if lowered == "/ns" or lowered == "ns":
            return ""
        if lowered.startswith("/ns "):
            return normalized[4:].strip()
        if lowered.startswith("ns "):
            return normalized[3:].strip()
        return normalized

    def _extract_remainder(self, event: AstrMessageEvent, raw: str = "") -> str:
        message = self._strip_ns_prefix(event.message_str or "")
        raw = raw.strip()
        if raw and len(raw.split()) >= len(message.split()):
            return self._strip_ns_prefix(raw)
        return message

    @staticmethod
    def _argument_after(text: str, prefix: str) -> str:
        value = (text or "").strip()
        if value.lower().startswith(prefix.lower()):
            return value[len(prefix) :].strip()
        return ""

    async def _run_job(self, event: AstrMessageEvent, job_path: str, payload: dict | None = None):
        logger.info("NS Ops job requested: %s by %s", job_path, event.get_sender_id())
        response = await self.client.run_job(job_path, payload)
        yield event.plain_result(format_job_response(response, self.max_output_chars))

    async def _run_job_text(self, job_path: str, payload: dict | None = None) -> str:
        response = await self.client.run_job(job_path, payload)
        return format_job_response(response, self.max_output_chars)

    async def _run_job_raw_text(self, job_path: str, payload: dict | None = None) -> str:
        response = await self.client.run_job(job_path, payload)
        if not response.get("ok"):
            return format_job_response(response, self.max_output_chars)
        result = response.get("result") or {}
        steps = result.get("steps") or []
        if steps and steps[0].get("stdout"):
            return str(steps[0].get("stdout") or "").strip()
        return str(result.get("output") or result.get("summary") or "").strip()

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        return not str(event.get_group_id() or "").strip()

    async def _traffic_report_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self._maybe_send_traffic_report()
                await self._maybe_send_health_alert()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("NS Ops traffic report loop error: %s", error)
            await asyncio.sleep(60)

    async def _maybe_send_traffic_report(self) -> None:
        origin = str(await self.get_kv_data("traffic_report_origin", "") or "").strip()
        if not origin:
            return

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour = int(self.config.get("traffic_report_hour", 12) or 12)
        minute = int(self.config.get("traffic_report_minute", 0) or 0)
        if (now.hour, now.minute) < (hour, minute):
            return

        today = now.date().isoformat()
        last_date = str(await self.get_kv_data("traffic_report_last_date", "") or "")
        if last_date == today:
            return

        text = await self._run_job_raw_text("system/daily")
        await self.context.send_message(
            origin,
            MessageChain([Comp.Plain(text)]),
        )
        await self.put_kv_data("traffic_report_last_date", today)

    async def _maybe_send_health_alert(self) -> None:
        if not self.config.get("health_alert_enabled", True):
            return

        origin = str(await self.get_kv_data("traffic_report_origin", "") or "").strip()
        if not origin:
            return

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        interval_seconds = int(self.config.get("health_alert_interval_seconds", 300) or 300)
        last_checked = float(await self.get_kv_data("health_alert_last_checked_at", "0") or 0)
        if now.timestamp() - last_checked < interval_seconds:
            return
        await self.put_kv_data("health_alert_last_checked_at", str(now.timestamp()))

        try:
            text = await self._run_job_raw_text("system/alerts")
        except Exception as error:
            text = "\n".join(
                [
                    "NS 异常告警",
                    f"签名：runner:{error}",
                    "",
                    f"- NS Ops Runner 调用失败：{error}",
                ]
            )

        normalized = text.strip()
        last_signature = str(await self.get_kv_data("health_alert_last_signature", "") or "")
        if normalized == "OK":
            if last_signature:
                await self.context.send_message(
                    origin,
                    MessageChain([Comp.Plain("NS 异常恢复\n\n当前健康检查正常。")]),
                )
                await self.put_kv_data("health_alert_last_signature", "")
            return

        signature = normalized
        for line in normalized.splitlines():
            if line.startswith("签名："):
                signature = line.removeprefix("签名：").strip()
                break
        if signature == last_signature:
            return

        await self.context.send_message(origin, MessageChain([Comp.Plain(normalized)]))
        await self.put_kv_data("health_alert_last_signature", signature)

    @filter.command("ns")
    async def handle_ns(self, event: AstrMessageEvent, raw: str = ""):
        if not self._is_allowed(event):
            yield event.plain_result("权限不足：/ns 仅限已授权管理员使用。")
            return

        remainder = self._extract_remainder(event, raw)
        if not remainder or remainder in {"help", "帮助"}:
            yield event.plain_result(format_help())
            return

        parts = remainder.split()
        head = parts[0].lower()
        tail = parts[1:]
        tail_lower = [part.lower() for part in tail]

        try:
            if head == "ping":
                payload = await self.client.health()
                yield event.plain_result("pong" if payload.get("ok") else "runner 不可用")
                return

            if head == "status":
                payload = await self.client.health()
                yield event.plain_result(format_health(payload, self.max_output_chars))
                return

            if head == "health":
                async for result in self._run_job(event, "system/health"):
                    yield result
                return

            if head == "daily":
                async for result in self._run_job(event, "system/daily"):
                    yield result
                return

            if head in {"alert", "alerts"}:
                async for result in self._run_job(event, "system/alerts"):
                    yield result
                return

            if head == "confirm":
                token = tail[0] if tail else ""
                if not token:
                    yield event.plain_result("用法：/ns confirm <验证码>")
                    return
                payload = await self.client.confirm(token)
                yield event.plain_result(format_job_response(payload, self.max_output_chars))
                return

            if head == "logs" and tail_lower[:1] == ["astrbot"]:
                async for result in self._run_job(event, "astrbot/logs"):
                    yield result
                return

            if head == "traffic":
                sub = tail_lower[0] if tail_lower else "today"
                if sub in {"today", "今日", "report"}:
                    async for result in self._run_job(event, "cloud/tencent/traffic/today"):
                        yield result
                    return
                if sub == "debug":
                    async for result in self._run_job(event, "cloud/tencent/traffic/debug"):
                        yield result
                    return
                if sub == "bind":
                    if not self._is_private(event):
                        yield event.plain_result("为了避免把运维日报推到群里，请私聊发送 /ns traffic bind。")
                        return
                    origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
                    if not origin:
                        yield event.plain_result("当前私聊来源无法记录，请稍后重试。")
                        return
                    await self.put_kv_data("traffic_report_origin", origin)
                    await self.put_kv_data("traffic_report_bound_by", str(event.get_sender_id()))
                    yield event.plain_result("已绑定当前私聊为 NS 每日流量报告接收窗口。默认每天 12:00 推送。")
                    return
                if sub == "unbind":
                    await self.put_kv_data("traffic_report_origin", "")
                    await self.put_kv_data("traffic_report_bound_by", "")
                    yield event.plain_result("已取消 NS 每日流量报告推送。")
                    return
                if sub == "status":
                    origin = str(await self.get_kv_data("traffic_report_origin", "") or "").strip()
                    last_date = str(await self.get_kv_data("traffic_report_last_date", "") or "-")
                    hour = int(self.config.get("traffic_report_hour", 12) or 12)
                    minute = int(self.config.get("traffic_report_minute", 0) or 0)
                    yield event.plain_result(
                        "\n".join(
                            [
                                "NS 每日流量报告",
                                f"状态：{'已绑定' if origin else '未绑定'}",
                                f"时间：{hour:02d}:{minute:02d}",
                                f"上次推送日期：{last_date}",
                            ]
                        )
                    )
                    return
                yield event.plain_result(
                    "用法：/ns traffic today | /ns traffic debug | /ns traffic bind | /ns traffic status | /ns traffic unbind"
                )
                return

            if head == "v2" and tail_lower[:1] == ["status"]:
                async for result in self._run_job(event, "v2/status"):
                    yield result
                return

            if head == "v2" and tail_lower[:1] == ["check"]:
                async for result in self._run_job(event, "v2/check"):
                    yield result
                return

            if head == "v2" and tail_lower[:1] == ["build"]:
                async for result in self._run_job(event, "v2/build"):
                    yield result
                return

            if head == "v2" and tail_lower[:1] == ["deploy"]:
                async for result in self._run_job(event, "v2/deploy"):
                    yield result
                return

            if head == "git" and tail_lower[:1] == ["status"]:
                async for result in self._run_job(event, "git/status"):
                    yield result
                return

            if head == "git" and tail_lower[:1] == ["diff"]:
                async for result in self._run_job(event, "git/diff"):
                    yield result
                return

            if head == "git" and tail_lower[:1] == ["commit"]:
                message = self._argument_after(remainder, "git commit")
                async for result in self._run_job(event, "git/commit", {"message": message}):
                    yield result
                return

            if head == "git" and tail_lower[:1] == ["push"]:
                async for result in self._run_job(event, "git/push"):
                    yield result
                return

            if head == "file" and tail_lower[:1] == ["write"]:
                args = self._argument_after(remainder, "file write")
                if not args or len(args.split(maxsplit=1)) < 2:
                    yield event.plain_result("用法：/ns file write <文件名.md> <内容>")
                    return
                relative_path, content = args.split(maxsplit=1)
                async for result in self._run_job(
                    event,
                    "file/write",
                    {"relativePath": relative_path, "content": content},
                ):
                    yield result
                return

            if head == "armoire" and tail_lower[:1] == ["check-store"]:
                async for result in self._run_job(event, "armoire/check-store"):
                    yield result
                return

            if head == "armoire" and tail_lower[:1] == ["audit-store"]:
                async for result in self._run_job(event, "armoire/audit-store"):
                    yield result
                return

            if head == "armoire" and tail_lower[:1] == ["audit-store-latest"]:
                async for result in self._run_job(event, "armoire/audit-store-latest"):
                    yield result
                return

            if head == "armoire" and tail_lower[:1] == ["sync-catalog"]:
                async for result in self._run_job(event, "armoire/sync-catalog"):
                    yield result
                return

            if head == "restart" and tail_lower[:1] == ["astrbot"]:
                async for result in self._run_job(event, "restart/astrbot"):
                    yield result
                return

            yield event.plain_result(format_help())
        except Exception as error:
            logger.error("NS Ops command failed: %s", error)
            yield event.plain_result(f"NS Ops 调用失败：{error}")
