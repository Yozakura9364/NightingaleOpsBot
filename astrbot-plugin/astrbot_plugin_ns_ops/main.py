from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
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

            if head == "restart" and tail_lower[:1] == ["astrbot"]:
                async for result in self._run_job(event, "restart/astrbot"):
                    yield result
                return

            yield event.plain_result(format_help())
        except Exception as error:
            logger.error("NS Ops command failed: %s", error)
            yield event.plain_result(f"NS Ops 调用失败：{error}")
