from __future__ import annotations

from dataclasses import dataclass


class CommandParseError(ValueError):
    pass


@dataclass(frozen=True)
class ShortLinkCommand:
    action: str
    code: str = ""
    target_url: str = ""


def parse_short_link_command(value: str) -> ShortLinkCommand:
    raw = str(value or "").strip()
    if not raw:
        return ShortLinkCommand("help")
    if raw.startswith(("http://", "https://")):
        return ShortLinkCommand("create", target_url=raw)

    parts = raw.split(maxsplit=2)
    action = parts[0].lower()

    if action in {"help", "帮助", "菜单"}:
        return ShortLinkCommand("help")
    if action in {"list", "列表"}:
        return ShortLinkCommand("list")

    if action in {"custom", "自定义"}:
        if len(parts) < 3:
            raise CommandParseError("用法：/短链 自定义 card https://目标地址")
        return ShortLinkCommand("create", code=parts[1], target_url=parts[2])

    if action in {"update", "修改", "更新"}:
        if len(parts) < 3:
            raise CommandParseError("用法：/短链 修改 card https://新地址")
        return ShortLinkCommand("update", code=parts[1], target_url=parts[2])

    unary_actions = {
        "disable": "disable",
        "停用": "disable",
        "关闭": "disable",
        "enable": "enable",
        "启用": "enable",
        "开启": "enable",
        "delete": "delete",
        "del": "delete",
        "删除": "delete",
    }
    if action in unary_actions:
        if len(parts) < 2:
            raise CommandParseError(f"用法：/短链 {parts[0]} card")
        return ShortLinkCommand(unary_actions[action], code=parts[1])

    if len(parts) >= 2 and parts[1].startswith(("http://", "https://")):
        target_url = parts[1] if len(parts) == 2 else f"{parts[1]} {parts[2]}"
        return ShortLinkCommand("create", code=parts[0], target_url=target_url)

    raise CommandParseError("无法识别命令，请发送 /短链 帮助。")
