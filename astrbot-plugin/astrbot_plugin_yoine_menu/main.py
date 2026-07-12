from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


PUBLIC_INDEX = """Yoine 菜单

/yoine 签到  账号绑定、签到类
/yoine 推送  日程、GitHub、B站、X、链接转图
/yoine 全部  分段发送全部公开菜单

账号凭证请私聊操作；群里不会接收 cookie、token、扫码登录凭证。
"""


PUBLIC_MENU_PARTS = {
    "签到": """Yoine 菜单：签到

石之家
/石之家帮助
/石之家绑定 [槽位]
/石之家状态 [槽位]
/石之家签到 [槽位|全部]
/石之家房屋 [槽位|全部]
/石之家改名 旧槽位 新槽位
/石之家解绑 槽位
别名：/绑定石之家 /石之家扫码绑定 /扫码绑定石之家 /解绑石之家 /改名石之家

盛趣积分商城
/盛趣商城帮助
/盛趣商城绑定 [槽位]  （只能用微信扫码）
/盛趣商城状态 [槽位]
/盛趣商城签到 [槽位|全部]
/盛趣商城改名 旧槽位 新槽位
/盛趣商城解绑 槽位
别名：/盛趣绑定 /商城绑定 /盛趣状态 /商城状态 /盛趣签到 /商城签到 /盛趣改名 /商城改名 /盛趣解绑 /商城解绑
""",
    "推送": """Yoine 菜单：推送与链接

日程提醒
/ddl 帮助
/ddl 添加 国服活动 2026-08-01 23:59 活动名称
/ddl 添加 国际服活动 2026-08-01 活动名称
/ddl 列表
/ddl 今日
/ddl 删除 3
/ddl 关
/ddl 开
/ddl 广播加入
/ddl 广播添加 国服活动 2026-08-01 23:59 活动名称
/ddl 广播列表
别名：/日程

FF14 官方更新提醒
/ff14watch 帮助
/ff14watch 订阅 新闻
/ff14watch 订阅 商城
/ff14watch 订阅列表
/ff14watch 测试
/ff14watch 源
/ff14watch 源 jp-news off
/ff14watch 关

GitHub 仓库更新提醒
/ghwatch 帮助
/ghwatch preset
/ghwatch preset show ffxiv-datamining
/ghwatch preset ffxiv-datamining
/ghwatch 订阅 owner/repo [branch]
/ghwatch 取消 owner/repo
/ghwatch 列表
/ghwatch 检查
/ghwatch 测试 owner/repo
/ghwatch 事件 owner/repo push on
/ghwatch 事件 owner/repo release off
/ghwatch 关

GitHub 链接卡片
发送 https://github.com/owner/repo 会自动解析卡片
/ghlink on
/ghlink off
/ghissue owner/repo#123
/ghpr owner/repo#123
/ghreadme owner/repo
/ghlimit

X 推送
/x帮助
/x订阅 @账号
/x取消订阅 @账号
/x订阅列表
/x推送测试 @账号
/x推送开
/x推送关
/x翻译状态

B站动态订阅
/bili_help
/bili_sub UID [关键词]
/bili_sub_list
/bili_sub_del UID
/bili_sub_test UID
/bili_sub_on
/bili_sub_off
/bili_bind  （请私聊发送 Cookie）
/bili_status
/bili_unbind
别名：/B站帮助 /B站绑定 /B站状态 /B站解绑 /订阅动态 /订阅列表 /订阅删除 /订阅测试

QQ 分享链接
/分享链接帮助
/链接解析状态
/转图 <链接>
/链接转图 <链接>
/nga状态
""",
}


ADMIN_INDEX = """Yoine 管理菜单

/yoine 管理运维  NS 运维、HAPI
"""


ADMIN_PRIVATE_MENU_PARTS = {
    "管理运维": """Yoine 管理菜单：运维

HAPI 远程控制（管理员；已安装，启用后可用）
/hapi help [主题]
/hapi list
/hapi list all
/hapi sw <序号或ID前缀>
/hapi s
/hapi msg [轮数]
/hapi to <序号> <内容>
/hapi pending
/hapi a
/hapi allow [序号]
/hapi answer [序号]
/hapi deny [序号]
/hapi create
/hapi abort [序号或ID前缀]
/hapi files [路径]
/hapi find <关键词>
/hapi download <路径>
/hapi upload [cancel]
/hapi bind [claude|codex|gemini|status|reset]
/hapi routes
快捷：>消息 或 >序号 消息

NS 运维（管理员）
/ns help
/ns ping
/ns status
/ns health
/ns daily
/ns logs astrbot
/ns traffic today
/ns traffic status
/ns v2 status
/ns v2 check
/ns v2 build
/ns v2 deploy
/ns git status
/ns git diff
/ns git commit <提交说明>
/ns git push
/ns file write <文件名.md> <内容>
/ns armoire check-store
/ns armoire audit-store
/ns armoire audit-store-latest
/ns armoire sync-catalog
/ns restart astrbot
""",
}


ALIASES = {
    "账号": "签到",
    "绑定": "签到",
    "签到": "签到",
    "推送": "推送",
    "订阅": "推送",
    "日程": "推送",
    "github": "推送",
    "GitHub": "推送",
    "b站": "推送",
    "B站": "推送",
    "bili": "推送",
    "仓库": "推送",
    "链接": "推送",
    "管理": "管理",
    "admin": "管理",
    "管理运维": "管理运维",
    "运维": "管理运维",
}


def _is_private(event: AstrMessageEvent) -> bool:
    return not str(event.get_group_id() or "").strip()


def _command_argument(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    first_line = value.splitlines()[0].strip()
    if first_line.startswith("/"):
        first_line = first_line[1:].lstrip()
    if first_line == "yoine":
        return ""
    if first_line.startswith("yoine "):
        return first_line[len("yoine ") :].strip()
    return ""


@register(
    "astrbot_plugin_yoine_menu",
    "NightingaleSilence",
    "Yoine 机器人命令菜单。",
    "0.1.0",
)
class YoineMenuPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    def _is_admin_private(self, event: AstrMessageEvent) -> bool:
        if not _is_private(event):
            return False

        sender_id = str(event.get_sender_id())
        try:
            config = self.context.get_config(event.unified_msg_origin)
            admin_ids = {str(item) for item in config.get("admins_id", [])}
        except Exception:
            admin_ids = set()

        if admin_ids:
            return sender_id in admin_ids
        return bool(event.is_admin())

    @filter.command("yoine")
    async def yoine_menu(self, event: AstrMessageEvent):
        argument = _command_argument(event.message_str or "")
        section = ALIASES.get(argument, argument)

        if not section:
            text = PUBLIC_INDEX
            if self._is_admin_private(event):
                text = text.rstrip() + "\n\n" + ADMIN_INDEX
            yield event.plain_result(text)
            return

        if section in {"全部", "all"}:
            for text in PUBLIC_MENU_PARTS.values():
                yield event.plain_result(text)
            if self._is_admin_private(event):
                for text in ADMIN_PRIVATE_MENU_PARTS.values():
                    yield event.plain_result(text)
            return

        if section == "管理":
            if not self._is_admin_private(event):
                yield event.plain_result("管理菜单仅在管理员私聊中显示。")
                return
            yield event.plain_result(ADMIN_INDEX)
            return

        if section in ADMIN_PRIVATE_MENU_PARTS:
            if not self._is_admin_private(event):
                yield event.plain_result("管理菜单仅在管理员私聊中显示。")
                return
            yield event.plain_result(ADMIN_PRIVATE_MENU_PARTS[section])
            return

        if section in PUBLIC_MENU_PARTS:
            yield event.plain_result(PUBLIC_MENU_PARTS[section])
            return

        yield event.plain_result(PUBLIC_INDEX)
