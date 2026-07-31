from __future__ import annotations

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .client import ShortLinkClient, ShortLinkClientError
from .commands import CommandParseError, ShortLinkCommand, parse_short_link_command


def _split_ids(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return {
        part.strip()
        for part in str(value).replace("\n", ",").split(",")
        if part.strip()
    }


def _command_argument(text: str) -> str:
    value = str(text or "").strip().splitlines()[0].strip() if str(text or "").strip() else ""
    if value.startswith("/"):
        value = value[1:].lstrip()
    for command in ("shortlink", "短链"):
        if value.lower() == command:
            return ""
        if value.lower().startswith(command + " "):
            return value[len(command) :].strip()
    return value


def _help_text() -> str:
    return "\n".join(
        [
            "私人短链",
            "",
            "随机生成：/短链 https://目标地址",
            "自定义：/短链 自定义 card https://目标地址",
            "查看：/短链 列表",
            "修改：/短链 修改 card https://新地址",
            "停用：/短链 停用 card",
            "启用：/短链 启用 card",
            "删除：/短链 删除 card",
        ]
    )


@register(
    "astrbot_plugin_short_links",
    "NightingaleSilence",
    "仅限所有者私聊使用的短链管理插件。",
    "0.1.0",
)
class ShortLinksPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = ShortLinkClient(
            endpoint=self.config.get(
                "service_endpoint", "http://host.docker.internal:18768"
            ),
            token=self.config.get("api_token", ""),
            timeout_seconds=int(self.config.get("timeout_seconds", 12) or 12),
        )
        self.max_output_chars = max(
            500, int(self.config.get("max_output_chars", 3000) or 3000)
        )

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        owners = _split_ids(self.config.get("owner_user_ids", ""))
        return bool(owners) and str(event.get_sender_id()) in owners

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        return not str(event.get_group_id() or "").strip()

    def _format_list(self, links: list[dict]) -> str:
        if not links:
            return "还没有短链。"
        lines = ["私人短链"]
        for link in links:
            code = str(link.get("code") or "")
            short_url = str(link.get("short_url") or "")
            target_url = str(link.get("target_url") or "")
            state = "启用" if link.get("enabled") else "停用"
            lines.extend(["", f"{code}（{state}）", short_url, f"→ {target_url}"])
        text = "\n".join(lines)
        if len(text) <= self.max_output_chars:
            return text
        return text[: self.max_output_chars] + "\n...[已截断]"

    @staticmethod
    def _format_result(prefix: str, link: dict) -> str:
        return "\n".join(
            [
                prefix,
                str(link.get("short_url") or ""),
                "",
                f"目标：{str(link.get('target_url') or '')}",
            ]
        ).strip()

    async def _execute(self, command: ShortLinkCommand) -> str:
        if command.action == "help":
            return _help_text()
        if command.action == "list":
            return self._format_list(await asyncio.to_thread(self.client.list))
        if command.action == "create":
            link = await asyncio.to_thread(
                self.client.create, command.target_url, command.code
            )
            return self._format_result("已生成短链：", link)
        if command.action == "update":
            link = await asyncio.to_thread(
                self.client.update, command.code, target_url=command.target_url
            )
            return self._format_result("已修改短链：", link)
        if command.action in {"disable", "enable"}:
            enabled = command.action == "enable"
            link = await asyncio.to_thread(
                self.client.update, command.code, enabled=enabled
            )
            return self._format_result("短链已启用：" if enabled else "短链已停用：", link)
        if command.action == "delete":
            await asyncio.to_thread(self.client.delete, command.code)
            return f"已删除短链：{command.code}"
        return _help_text()

    @filter.command("短链", alias={"shortlink"})
    async def short_links(self, event: AstrMessageEvent):
        if not self._is_private(event):
            yield event.plain_result("短链管理只能在私聊中使用。")
            return
        if not self._is_owner(event):
            logger.warning("Rejected short-link command from sender %s", event.get_sender_id())
            yield event.plain_result("权限不足。")
            return
        try:
            command = parse_short_link_command(_command_argument(event.message_str or ""))
            result = await self._execute(command)
        except CommandParseError as error:
            result = str(error)
        except ShortLinkClientError as error:
            logger.warning("Short-link command failed with status %s", error.status)
            result = str(error)
        except Exception as error:
            logger.error("Short-link command failed: %s", error)
            result = "短链操作失败，请检查服务状态。"
        yield event.plain_result(result)
