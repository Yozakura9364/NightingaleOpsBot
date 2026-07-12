from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, unquote, urlparse, urlunparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .card_renderer import CardContent, render_card
from .web_card import check_nga_cookie_status, fetch_nga_thread_card, fetch_url_card, nga_cookie_file_path, platform_label


URL_RE = re.compile(r"https?://[^\s<>'\"`，。！？、；：）)\]}]+", re.IGNORECASE)
URL_FIELD_HINTS = ("url", "link", "jump", "target", "source", "web", "page", "share")
MEDIA_FIELD_HINTS = ("pic", "image", "img", "icon", "avatar", "cover", "thumb", "preview")
MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
}
TITLE_FIELD_HINTS = ("title", "desc", "summary", "prompt")
TRACKING_PARAM_PREFIXES = ("utm_",)
DEFAULT_TRACKING_PARAMS = {
    "app_platform",
    "app_version",
    "ignoreengage",
    "share_from_user_hidden",
    "type",
    "author_share",
    "shareredid",
    "apptime",
    "share_id",
    "share_channel",
    "timestamp",
    "from",
    "from_user",
    "sharefrom",
    "share_source",
    "share_medium",
}


@dataclass(frozen=True)
class LinkCandidate:
    url: str
    priority: int
    order: int
    source_key: str = ""


def _split_items(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    return {part.strip().lower() for part in str(value).replace("\n", ",").split(",") if part.strip()}


def _bool_config(config: AstrBotConfig, key: str, default: bool) -> bool:
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "开启", "是"}
    return bool(value)


def _int_config(config: AstrBotConfig, key: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(config.get(key, default) or default))
    except Exception:
        return default


def _platform_ignored(url: str, ignored_platforms: set[str]) -> bool:
    if not ignored_platforms:
        return False
    return platform_label(url).strip().lower() in ignored_platforms


def _event_group_id(event: AstrMessageEvent) -> str:
    return str(event.get_group_id() or "").strip()


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
    return ""


def _component_type_name(component: Any) -> str:
    value = getattr(component, "type", "")
    return str(getattr(value, "value", value) or component.__class__.__name__).lower()


def _is_json_component(component: Any) -> bool:
    return _component_type_name(component) == "json" or component.__class__.__name__.lower() == "json"


def _message_components(event: AstrMessageEvent) -> list[Any]:
    message_obj = getattr(event, "message_obj", None)
    message = getattr(message_obj, "message", None)
    if isinstance(message, list):
        return message
    if message is not None:
        try:
            return list(message)
        except TypeError:
            return []
    return []


def _normalize_text(value: str) -> str:
    text = html.unescape(str(value or "")).replace("\\/", "/").strip()
    for _ in range(2):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _clean_url(value: str) -> str:
    url = _normalize_text(value)
    return url.rstrip(".,;:!?，。！？、；：)]}）")


def _clean_share_url(url: str, tracking_params: set[str]) -> str:
    cleaned = _clean_url(url)
    xhs_url = _normalize_xiaohongshu_url(cleaned)
    if xhs_url:
        return xhs_url
    miyoushe_url = _normalize_miyoushe_url(cleaned)
    if miyoushe_url:
        return miyoushe_url
    if not tracking_params:
        return cleaned
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return cleaned
    kept_params = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in tracking_params or any(lowered.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES):
            continue
        kept_params.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(kept_params, doseq=True), fragment=""))


def _normalize_xiaohongshu_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not (hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com")):
        return ""
    if "/discovery/item/" not in parsed.path:
        return ""

    params = {key.lower(): value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
    query = [
        ("source", "webshare"),
        ("xhsshare", "pc_web"),
    ]
    xsec_token = params.get("xsec_token", "")
    if xsec_token:
        query.append(("xsec_token", xsec_token))
    query.append(("xsec_source", "pc_share"))
    return urlunparse(
        parsed._replace(
            scheme="https",
            netloc="www.xiaohongshu.com",
            query=urlencode(query, doseq=True),
            fragment="",
        )
    )


def _normalize_miyoushe_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not (hostname == "miyoushe.com" or hostname.endswith(".miyoushe.com")):
        return ""

    path_parts = [part for part in parsed.path.split("/") if part]
    channel = path_parts[0] if path_parts else ""
    article_id = ""

    if len(path_parts) >= 3 and path_parts[1] == "article" and path_parts[2].isdigit():
        article_id = path_parts[2]
    else:
        fragment_parts = [part for part in parsed.fragment.split("/") if part]
        if len(fragment_parts) >= 2 and fragment_parts[0] == "article" and fragment_parts[1].isdigit():
            article_id = fragment_parts[1]

    if not channel or not article_id:
        return ""

    return f"https://www.miyoushe.com/{channel}/article/{article_id}"


def _domain_ignored(hostname: str, ignored_domains: set[str]) -> bool:
    host = hostname.lower().strip(".")
    return any(host == domain or host.endswith("." + domain) for domain in ignored_domains)


def _is_media_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if any(path.endswith(extension) for extension in MEDIA_EXTENSIONS):
        return True
    return any(part in path for part in ("/image/", "/images/", "/avatar/", "/thumb/", "/cover/"))


def _is_media_candidate(candidate: LinkCandidate, ignored_domains: set[str]) -> bool:
    lowered_key = candidate.source_key.lower()
    if any(hint in lowered_key for hint in MEDIA_FIELD_HINTS):
        return True
    parsed = urlparse(_clean_url(candidate.url))
    hostname = parsed.hostname or ""
    return _domain_ignored(hostname, ignored_domains) or _is_media_url(candidate.url)


def _valid_url(url: str, ignored_domains: set[str], exclude_media_links: bool) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    hostname = parsed.hostname or ""
    if _domain_ignored(hostname, ignored_domains):
        return False
    if exclude_media_links and _is_media_url(url):
        return False
    return True


def _extract_urls_from_text(text: str) -> list[str]:
    normalized = _normalize_text(text)
    return [_clean_url(match.group(0)) for match in URL_RE.finditer(normalized)]


def _maybe_nested_json(text: str) -> Any | None:
    value = str(text or "").strip()
    if not value or value[0] not in "[{":
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _priority_for_key(key: str) -> int:
    lowered = key.lower()
    if any(hint in lowered for hint in URL_FIELD_HINTS):
        return 100
    if any(hint in lowered for hint in MEDIA_FIELD_HINTS):
        return 20
    return 50


def _collect_candidates(value: Any, *, key: str = "", order: list[int]) -> list[LinkCandidate]:
    candidates: list[LinkCandidate] = []
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            candidates.extend(_collect_candidates(child_value, key=str(child_key), order=order))
        return candidates
    if isinstance(value, list):
        for child_value in value:
            candidates.extend(_collect_candidates(child_value, key=key, order=order))
        return candidates
    if not isinstance(value, str):
        return candidates

    priority = _priority_for_key(key)
    for url in _extract_urls_from_text(value):
        order[0] += 1
        candidates.append(LinkCandidate(url=url, priority=priority, order=order[0], source_key=key))

    nested = _maybe_nested_json(value)
    if nested is not None:
        candidates.extend(_collect_candidates(nested, key=key, order=order))
    return candidates


def _extract_candidates(payloads: list[Any]) -> list[LinkCandidate]:
    candidates: list[LinkCandidate] = []
    order = [0]
    for payload in payloads:
        candidates.extend(_collect_candidates(payload, order=order))
    return candidates


def _extract_links(
    payloads: list[Any],
    ignored_domains: set[str],
    exclude_media_links: bool,
    max_links: int,
    tracking_params: set[str] | None = None,
) -> list[str]:
    candidates = _extract_candidates(payloads)
    if tracking_params is None:
        tracking_params = DEFAULT_TRACKING_PARAMS

    sorted_candidates = sorted(candidates, key=lambda item: (-item.priority, item.order))
    result: list[str] = []
    seen: set[str] = set()
    for candidate in sorted_candidates:
        if exclude_media_links and _is_media_candidate(candidate, ignored_domains):
            continue
        url = _clean_share_url(candidate.url, tracking_params)
        if not _valid_url(url, ignored_domains, exclude_media_links):
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
        if len(result) >= max_links:
            break
    return result


def _extract_media_links(payloads: list[Any], ignored_domains: set[str], max_links: int) -> list[str]:
    candidates = sorted(_extract_candidates(payloads), key=lambda item: (item.order, -item.priority))
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = _clean_url(candidate.url)
        if not _is_media_candidate(candidate, ignored_domains):
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
        if len(result) >= max_links:
            break
    return result


def _text_looks_like_url(text: str) -> bool:
    return bool(URL_RE.search(text))


def _clean_title(text: str) -> str:
    value = html.unescape(str(text or "")).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:120]


def _extract_title(value: Any, *, key: str = "") -> str:
    if isinstance(value, dict):
        title_candidates: list[str] = []
        fallback_candidates: list[str] = []
        for child_key, child_value in value.items():
            child_key_text = str(child_key)
            child = _extract_title(child_value, key=child_key_text)
            if not child:
                continue
            if any(hint in child_key_text.lower() for hint in TITLE_FIELD_HINTS):
                title_candidates.append(child)
            else:
                fallback_candidates.append(child)
        return (title_candidates or fallback_candidates or [""])[0]
    if isinstance(value, list):
        for child_value in value:
            child = _extract_title(child_value, key=key)
            if child:
                return child
        return ""
    if not isinstance(value, str):
        return ""

    lowered_key = key.lower()
    if not any(hint in lowered_key for hint in TITLE_FIELD_HINTS):
        return ""
    text = _clean_title(value)
    if not text or _text_looks_like_url(text) or text.startswith("{"):
        return ""
    return text


def _extract_first_title(payloads: list[Any]) -> str:
    for payload in payloads:
        title = _extract_title(payload)
        if title:
            return title
    return ""


def _extract_first_description(payloads: list[Any]) -> str:
    for payload in payloads:
        description = _extract_description(payload)
        if description:
            return description
    return ""


def _best_card_title(fetched: CardContent | None, payloads: list[Any], source: str) -> str:
    payload_title = _extract_first_title(payloads)
    fetched_title = _clean_title(fetched.title) if fetched else ""
    if fetched_title and fetched_title != source and not _title_looks_like_http_error(fetched_title):
        return fetched_title
    return payload_title or fetched_title or source


def _title_looks_like_http_error(value: str) -> bool:
    return bool(re.fullmatch(r"[45]\d{2}(?:\s+\w+)?", _clean_title(value), flags=re.IGNORECASE))


def _extract_description(value: Any, *, key: str = "") -> str:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            child = _extract_description(child_value, key=str(child_key))
            if child:
                return child
        return ""
    if isinstance(value, list):
        for child_value in value:
            child = _extract_description(child_value, key=key)
            if child:
                return child
        return ""
    if not isinstance(value, str):
        return ""
    lowered_key = key.lower()
    if not any(hint in lowered_key for hint in ("desc", "summary", "digest", "content")):
        return ""
    text = _clean_title(value)
    if not text or text.startswith("{") or _text_looks_like_url(text):
        return ""
    return text[:500]


def _format_links(links: list[str], prefix: str, title: str = "") -> str:
    lines: list[str] = []
    if title:
        lines.extend([f"标题：{title}", ""])
    if len(links) == 1:
        lines.extend([prefix, links[0]])
        return "\n".join(lines)
    lines.append(prefix)
    lines.extend(f"{index}. {url}" for index, url in enumerate(links, start=1))
    return "\n".join(lines)


def _help_text() -> str:
    return "\n".join(
        [
            "QQ 分享链接还原",
            "",
            "自动解析 QQ 手机分享卡片里的原链接，并发送为普通文本链接。",
            "",
            "命令：",
            "/分享链接帮助",
            "/链接解析状态",
            "/转图 <链接>",
            "/链接转图 <链接>",
            "/nga状态",
            "",
            "说明：默认只处理 JSON 分享卡片，不会重复解析普通文本链接。",
            "输出：优先显示卡片标题，清理分享追踪参数，并把卡片配图作为图片发送。",
            "长图：分享卡片会优先用卡片里的标题和封面生成图；命令抓取失败时会降级为普通链接卡片。",
        ]
    )


@register(
    "astrbot_plugin_share_link_resolver",
    "NightingaleSilence",
    "QQ 分享卡片原链接还原插件。",
    "0.1.0",
)
class ShareLinkResolverPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(__file__).resolve().parent / ".local"
        self.card_dir = self.data_dir / "cards"

    def _allowed(self, event: AstrMessageEvent) -> bool:
        group_id = _event_group_id(event)
        if group_id:
            if not _bool_config(self.config, "allow_groups", True):
                return False
            blocked = _split_items(self.config.get("blocked_group_ids", ""))
            if group_id in blocked:
                return False
            allowed = _split_items(self.config.get("allowed_group_ids", ""))
            return not allowed or group_id in allowed
        return _bool_config(self.config, "allow_private", True)

    def _json_payloads(self, event: AstrMessageEvent) -> list[Any]:
        payloads: list[Any] = []
        for component in _message_components(event):
            if not _is_json_component(component):
                continue
            data = getattr(component, "data", None)
            if data is not None:
                payloads.append(data)
        return payloads

    def _plain_payloads(self, event: AstrMessageEvent) -> list[str]:
        if not _bool_config(self.config, "include_plain_text", False):
            return []
        text = str(event.message_str or "").strip()
        return [text] if text else []

    @filter.command("分享链接帮助")
    async def share_link_help(self, event: AstrMessageEvent):
        yield event.plain_result(_help_text())

    @filter.command("链接解析状态")
    async def share_link_status(self, event: AstrMessageEvent):
        status = "开启" if _bool_config(self.config, "enabled", True) else "关闭"
        groups = "开启" if _bool_config(self.config, "allow_groups", True) else "关闭"
        private = "开启" if _bool_config(self.config, "allow_private", True) else "关闭"
        plain = "开启" if _bool_config(self.config, "include_plain_text", False) else "关闭"
        images = "开启" if _bool_config(self.config, "send_images", True) else "关闭"
        cards = "开启" if _bool_config(self.config, "render_card_images", True) else "关闭"
        clean = "开启" if _bool_config(self.config, "clean_share_params", True) else "关闭"
        stop = "开启" if _bool_config(self.config, "stop_after_resolved", True) else "关闭"
        yield event.plain_result(
            "\n".join(
                [
                    f"分享链接解析：{status}",
                    f"群聊解析：{groups}",
                    f"私聊解析：{private}",
                    f"普通文本解析：{plain}",
                    f"分享参数清理：{clean}",
                    f"卡片配图发送：{images}",
                    f"链接长图生成：{cards}",
                    f"解析后阻止后续插件：{stop}",
                ]
            )
        )

    @filter.command("转图")
    async def render_link_card(self, event: AstrMessageEvent):
        async for result in self._handle_render_command(event, ("转图",)):
            yield result

    @filter.command("链接转图")
    async def render_link_card_alias(self, event: AstrMessageEvent):
        async for result in self._handle_render_command(event, ("链接转图",)):
            yield result

    @filter.command("nga状态")
    async def nga_status(self, event: AstrMessageEvent):
        status = await check_nga_cookie_status(timeout_seconds=_int_config(self.config, "fetch_timeout_seconds", 12))
        exists = "存在" if status.get("exists") else "不存在"
        logged_in = "是" if status.get("logged_in") else "否"
        uid = str(status.get("uid") or "")
        mode = str(status.get("mode") or "游客")
        ok = "成功" if status.get("ok") else "未完成"
        error = str(status.get("error") or "")
        lines = [
            "NGA 抓取状态",
            f"Cookie 文件：{exists}",
            f"Cookie 数量：{status.get('cookie_count', 0)}",
            f"访问测试：{ok}",
            f"登录态：{logged_in}",
            f"模式：{mode}",
        ]
        if uid:
            lines.append(f"当前 UID：{uid}")
        if error:
            lines.append(f"错误类型：{error}")
        lines.append(f"服务器文件：{nga_cookie_file_path()}")
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=20)
    async def resolve_share_link(self, event: AstrMessageEvent):
        if not _bool_config(self.config, "enabled", True) or not self._allowed(event):
            return

        payloads = self._json_payloads(event)
        has_json_payload = bool(payloads)
        payloads.extend(self._plain_payloads(event))
        if not payloads:
            return

        links = _extract_links(
            payloads,
            ignored_domains=_split_items(self.config.get("ignored_domains", "")),
            exclude_media_links=_bool_config(self.config, "exclude_media_links", True),
            max_links=_int_config(self.config, "max_links_per_message", 3),
            tracking_params=self._tracking_params(),
        )
        links = [
            link
            for link in links
            if not _platform_ignored(link, _split_items(self.config.get("ignored_platforms", "bilibili")))
        ]
        if not links:
            if has_json_payload and _bool_config(self.config, "debug_log", False):
                logger.info("QQ share link resolver found no usable links.")
            return

        if _bool_config(self.config, "debug_log", False):
            logger.info("QQ share link resolver extracted %s link(s).", len(links))
        if _bool_config(self.config, "stop_after_resolved", True):
            event.stop_event()

        text = _format_links(links, str(self.config.get("reply_prefix", "解析出直链：") or ""), _extract_first_title(payloads))
        images = []
        if _bool_config(self.config, "send_images", True):
            images = _extract_media_links(
                payloads,
                ignored_domains=_split_items(self.config.get("image_domains", "qq.ugcimg.cn,qpic.cn,gtimg.cn")),
                max_links=_int_config(self.config, "max_images_per_message", 1),
            )
        if _bool_config(self.config, "render_card_images", True):
            card_path = await self._render_payload_card(payloads, links[0], images[0] if images else "")
            if card_path:
                await event.send(MessageChain([Comp.Plain(text)]))
                await event.send(MessageChain([Comp.Image.fromFileSystem(str(card_path))]))
                return

        if images:
            chain = [Comp.Plain(text)]
            chain.extend(Comp.Image.fromURL(url) for url in images)
            await event.send(MessageChain(chain))
            return
        yield event.plain_result(text)

    def _tracking_params(self) -> set[str]:
        if not _bool_config(self.config, "clean_share_params", True):
            return set()
        configured = _split_items(self.config.get("tracking_params", ""))
        return configured or DEFAULT_TRACKING_PARAMS

    async def _handle_render_command(self, event: AstrMessageEvent, commands: tuple[str, ...]):
        argument = _command_argument(event.message_str or "", commands)
        urls = _extract_urls_from_text(argument)
        if not urls:
            yield event.plain_result("用法：/转图 <链接>")
            return

        url = _clean_share_url(urls[0], self._tracking_params())
        try:
            content = await fetch_url_card(
                url,
                timeout_seconds=_int_config(self.config, "fetch_timeout_seconds", 12),
                max_bytes=_int_config(self.config, "fetch_max_bytes", 1500000, minimum=100000),
            )
        except Exception:
            content = CardContent(source=platform_label(url), title=platform_label(url), url=url, description="内容抓取失败，已生成链接卡片。")

        card_path = await self._render_card(content)
        await event.send(MessageChain([Comp.Plain(url)]))
        await event.send(MessageChain([Comp.Image.fromFileSystem(str(card_path))]))

    async def _render_payload_card(self, payloads: list[Any], url: str, cover_url: str) -> Path | None:
        try:
            source = platform_label(url)
            fetched: CardContent | None = None
            if source in {"NGA", "微博", "米游社", "TapTap", "库街区", "小黑盒"}:
                try:
                    timeout_seconds = _int_config(self.config, "fetch_timeout_seconds", 12)
                    max_bytes = _int_config(self.config, "fetch_max_bytes", 1500000, minimum=100000)
                    if source == "NGA":
                        fetched = await fetch_nga_thread_card(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
                    else:
                        fetched = await fetch_url_card(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
                    if _bool_config(self.config, "debug_log", False):
                        logger.info("QQ share card fetched %s content with %s post(s).", source, len(fetched.posts))
                except Exception as error:
                    logger.info("QQ share card fetch fallback for %s: %s", source, error)
            if fetched and fetched.posts:
                content = fetched
            elif source == "NGA":
                content = CardContent(
                    source=source,
                    title=(fetched.title if fetched else "") or _extract_first_title(payloads) or source,
                    url=url,
                    description=(fetched.description if fetched and fetched.description else "正文抓取失败，可打开链接查看。"),
                    cover_url="",
                    footer="NGA 抓取未获取到正文",
                    posts=(),
                )
            else:
                content = CardContent(
                    source=source,
                    title=_best_card_title(fetched, payloads, source),
                    url=url,
                    description=(fetched.description if fetched else "") or _extract_first_description(payloads),
                    cover_url=(fetched.cover_url if fetched else "") or cover_url,
                    footer="由 QQ 分享卡片生成",
                    posts=fetched.posts if fetched else (),
                )
            return await self._render_card(content)
        except Exception as error:
            logger.warning("QQ share card render failed: %s", error)
            return None

    async def _render_card(self, content: CardContent) -> Path:
        width = _int_config(self.config, "card_width", 760, minimum=480)
        max_height = _int_config(self.config, "card_max_height", 2200, minimum=600)
        return await asyncio.to_thread(render_card, content, self.card_dir, width=width, max_height=max_height)
