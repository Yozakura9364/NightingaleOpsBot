from __future__ import annotations

from urllib.parse import quote

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


ITEM_PAGE_PREFIX = "https://ff14.huijiwiki.com/wiki/物品:"
COMMAND_NAMES = frozenset({"item", "ff14item"})


def extract_item_name(message: str) -> str:
    raw = str(message or "").strip()
    if not raw:
        return ""

    parts = raw.split(maxsplit=1)
    command = parts[0].lstrip("/").lower()
    if command in COMMAND_NAMES:
        return parts[1].strip() if len(parts) > 1 else ""
    return raw


def build_item_url(item_name: str) -> str:
    return ITEM_PAGE_PREFIX + quote(item_name, safe="")


@register(
    "astrbot_plugin_huijiwiki",
    "NightingaleSilence",
    "查询 FF14 灰机 Wiki 物品页面。命令：/item，兼容 /ff14item。",
    "1.0.0",
)
class HuijiWikiPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("item", alias={"ff14item"})
    async def item(self, event: AstrMessageEvent):
        item_name = extract_item_name(event.message_str)
        if not item_name:
            yield event.plain_result(
                "用法：/item 物品名\n兼容：/ff14item 物品名\n示例：/item 夜音"
            )
            return

        yield event.plain_result(build_item_url(item_name))
