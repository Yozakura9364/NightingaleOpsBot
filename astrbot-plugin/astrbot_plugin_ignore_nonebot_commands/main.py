from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


DEFAULT_IGNORED_COMMANDS = {"nbping", "/nbping"}


def _split_items(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    return {
        part.strip().lower()
        for part in str(value).replace("\n", ",").split(",")
        if part.strip()
    }


def _first_line(text: str) -> str:
    value = str(text or "").strip()
    return value.splitlines()[0].strip().lower() if value else ""


@register(
    "astrbot_plugin_ignore_nonebot_commands",
    "NightingaleSilence",
    "Stops AstrBot from answering commands reserved for NoneBot.",
    "0.1.0",
)
class IgnoreNoneBotCommandsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    def _ignored_sender_ids(self) -> set[str]:
        return _split_items(self.config.get("ignored_sender_ids", []))

    def _ignored_commands(self) -> set[str]:
        configured = _split_items(self.config.get("ignored_commands", ""))
        return configured or DEFAULT_IGNORED_COMMANDS

    def _ignored_prefixes(self) -> set[str]:
        return _split_items(self.config.get("ignored_prefixes", ""))

    def _should_ignore(self, text: str) -> bool:
        line = _first_line(text)
        if not line:
            return False

        commands = self._ignored_commands()
        if line in commands:
            return True

        first_token = line.split(maxsplit=1)[0]
        if first_token in commands:
            return True

        return any(line.startswith(prefix) for prefix in self._ignored_prefixes())

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def ignore_nonebot_commands(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id()).strip()
        if sender_id in self._ignored_sender_ids():
            logger.info(
                "Ignored message from blacklisted sender %s",
                sender_id,
            )
            event.stop_event()
            return

        if self._should_ignore(event.message_str or ""):
            logger.info(
                "Ignored NoneBot command for sender %s: %s",
                event.get_sender_id(),
                _first_line(event.message_str or ""),
            )
            event.stop_event()
