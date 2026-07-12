from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
import json
import re
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener, urlopen
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class FeedItem:
    item_id: str
    title: str
    link: str
    published_at: str
    summary: str
    author_name: str
    image_urls: list[str]
    avatar_url: str = ""


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_IMG_SRC_RE = re.compile(r"""<img\b[^>]*\bsrc=["']([^"']+)["']""", re.I)
_COOKIE_PART_RE = re.compile(r"(?i)^\s*([a-z0-9_]+)\s*[:=]\s*(.+?)\s*$")
_IMAGE_URL_RE = re.compile(r"\.(?:jpg|jpeg|png|gif|webp)(?:[?#].*)?$", re.I)
_DEFAULT_API_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


def normalize_uid(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("请提供 B 站 UID 或空间链接。")

    for pattern in (
        r"space\.bilibili\.com/(\d+)",
        r"\buid[:=]\s*(\d+)\b",
        r"\bUID[:=]\s*(\d+)\b",
    ):
        match = re.search(pattern, raw, flags=re.I)
        if match:
            return match.group(1)

    if re.fullmatch(r"\d{1,20}", raw):
        return raw
    raise ValueError("B 站 UID 格式不正确，请使用纯数字 UID 或 https://space.bilibili.com/123456 这样的链接。")


def normalize_cookie(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("B 站 Cookie 不能为空。")

    parts: list[str] = []
    seen: set[str] = set()
    for item in text.split(";"):
        match = _COOKIE_PART_RE.match(item.strip())
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if not key or not value:
            continue
        lowered = key.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        parts.append(f"{key}={value}")

    if not parts:
        raise ValueError("B 站 Cookie 格式不正确。")

    cookie = "; ".join(parts)
    required = ("sessdata", "bili_jct", "dedeuserid")
    missing = [name for name in required if f"{name}=" not in cookie.lower()]
    if missing:
        raise ValueError(f"缺少必要 Cookie：{', '.join(missing)}")
    return cookie


def extract_cookie_value(cookie: str, key: str) -> str:
    lowered_key = str(key or "").strip().lower()
    for item in str(cookie or "").split(";"):
        match = _COOKIE_PART_RE.match(item.strip())
        if match and match.group(1).strip().lower() == lowered_key:
            return match.group(2).strip()
    return ""


def fetch_user_feed(base_url: str, uid: str, *, timeout: float = 30.0) -> list[FeedItem]:
    normalized = normalize_uid(uid)
    url = _feed_url(base_url, normalized)
    request = Request(url, headers={"User-Agent": "NightingaleOpsBot-BiliFeed/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read(2_000_000)
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as error:
        body = error.read(20_000).decode("utf-8", errors="replace")
        raise RuntimeError(_http_error_message(error.code, body)) from error
    except URLError as error:
        raise RuntimeError(f"连接 RSSHub 失败：{error.reason}") from error

    if status >= 400:
        raise RuntimeError(f"RSSHub 返回 HTTP {status}")

    text = body.decode(_detect_charset(content_type) or "utf-8", errors="replace").strip()
    if not text:
        return []
    if text.lower().startswith("<!doctype html") or "<html" in text[:200].lower():
        raise RuntimeError(_html_error_message(text))

    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise RuntimeError(f"RSSHub 返回内容不是有效 RSS/XML：{error}") from error

    items = _parse_rss_items(root)
    if items:
        return items
    return _parse_atom_entries(root)


def fetch_cookie_user_feed(
    uid: str,
    *,
    cookie: str,
    user_agent: str = _DEFAULT_API_USER_AGENT,
    timeout: float = 30.0,
    proxy_url: str = "",
) -> list[FeedItem]:
    normalized_uid = normalize_uid(uid)
    normalized_cookie = normalize_cookie(cookie)
    url = (
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        f"?host_mid={quote(normalized_uid)}"
    )
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Cookie": normalized_cookie,
        "Origin": "https://space.bilibili.com",
        "Referer": f"https://space.bilibili.com/{normalized_uid}/dynamic",
        "User-Agent": str(user_agent or _DEFAULT_API_USER_AGENT).strip() or _DEFAULT_API_USER_AGENT,
    }
    handlers = []
    if proxy_url:
        handlers.append(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = build_opener(*handlers)
    request = Request(url, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read(2_000_000)
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as error:
        body = error.read(40_000).decode("utf-8", errors="replace")
        raise RuntimeError(_http_error_message(error.code, body)) from error
    except URLError as error:
        raise RuntimeError(f"连接 B 站动态接口失败：{error.reason}") from error

    if status >= 400:
        raise RuntimeError(f"B 站动态接口返回 HTTP {status}")

    text = body.decode(_detect_charset(content_type) or "utf-8", errors="replace").strip()
    if not text:
        return []
    if text.lower().startswith("<!doctype html") or "<html" in text[:200].lower():
        raise RuntimeError(_html_error_message(text))

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"B 站动态接口返回的不是有效 JSON：{error}") from error

    code = int(payload.get("code", -1))
    if code != 0:
        message = str(payload.get("message") or payload.get("msg") or f"code={code}").strip()
        if code == -101:
            raise RuntimeError("B 站登录态已失效，请重新绑定 Cookie。")
        if code == -352:
            raise RuntimeError(f"B 站风控校验失败：{message}")
        raise RuntimeError(f"B 站动态接口返回错误：{message} (code={code})")

    items = (((payload.get("data") or {}).get("items")) or [])
    parsed_items: list[FeedItem] = []
    for item in items:
        if _dynamic_is_pinned(item):
            continue
        parsed = _parse_dynamic_item(item)
        if parsed is not None:
            parsed_items.append(parsed)
    return parsed_items


def fetch_cookie_dynamic_detail(
    item_id: str,
    *,
    cookie: str,
    user_agent: str = _DEFAULT_API_USER_AGENT,
    timeout: float = 30.0,
    proxy_url: str = "",
) -> FeedItem | None:
    normalized_cookie = normalize_cookie(cookie)
    normalized_item_id = str(item_id or "").strip()
    if not normalized_item_id:
        return None

    url = (
        "https://api.bilibili.com/x/polymer/web-dynamic/desktop/v1/detail"
        f"?id={quote(normalized_item_id)}"
    )
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Cookie": normalized_cookie,
        "Origin": "https://space.bilibili.com",
        "Referer": f"https://t.bilibili.com/{normalized_item_id}",
        "User-Agent": str(user_agent or _DEFAULT_API_USER_AGENT).strip() or _DEFAULT_API_USER_AGENT,
    }
    handlers = []
    if proxy_url:
        handlers.append(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = build_opener(*handlers)
    request = Request(url, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read(2_000_000)
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as error:
        body = error.read(40_000).decode("utf-8", errors="replace")
        raise RuntimeError(_http_error_message(error.code, body)) from error
    except URLError as error:
        raise RuntimeError(f"连接 B站动态详情接口失败：{error.reason}") from error

    if status >= 400:
        raise RuntimeError(f"B站动态详情接口返回 HTTP {status}")

    text = body.decode(_detect_charset(content_type) or "utf-8", errors="replace").strip()
    if not text:
        return None
    if text.lower().startswith("<!doctype html") or "<html" in text[:200].lower():
        raise RuntimeError(_html_error_message(text))

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"B站动态详情接口返回的不是有效 JSON：{error}") from error

    code = int(payload.get("code", -1))
    if code != 0:
        message = str(payload.get("message") or payload.get("msg") or f"code={code}").strip()
        if code == -101:
            raise RuntimeError("B站登录态已失效，请重新绑定 Cookie。")
        if code == -352:
            raise RuntimeError(f"B站风控校验失败：{message}")
        raise RuntimeError(f"B站动态详情接口返回错误：{message} (code={code})")

    item = ((payload.get("data") or {}).get("item")) or {}
    return _parse_desktop_dynamic_item(item)


def _feed_url(base_url: str, uid: str) -> str:
    return f"{str(base_url or '').rstrip('/')}/bilibili/user/dynamic/{quote(uid)}"


def _detect_charset(content_type: str) -> str | None:
    match = re.search(r"charset=([^;\s]+)", content_type or "", flags=re.I)
    return match.group(1).strip("\"'") if match else None


def _http_error_message(status: int, body: str) -> str:
    detail = _html_error_message(body) if body else ""
    return f"HTTP {status}" + (f"：{detail}" if detail else "")


def _html_error_message(text: str) -> str:
    cleaned = str(text or "")
    match = re.search(
        r"Error Message:\s*<br\s*/?>\s*<code[^>]*>(.*?)</code>",
        cleaned,
        flags=re.I | re.S,
    )
    if match:
        return _clean_text(match.group(1))
    if "错误号: 412" in cleaned or "security control policy" in cleaned.lower():
        return "B 站风控拦截（412）"
    title = re.search(r"<title[^>]*>(.*?)</title>", cleaned, flags=re.I | re.S)
    return _clean_text(title.group(1)) if title else "返回了 HTML 错误页"


def _parse_rss_items(root: ET.Element) -> list[FeedItem]:
    channel = _first_child(root, "channel")
    source: Iterable[ET.Element] = channel if channel is not None else root
    feed_author = _clean_text(_child_text(channel, "title")) if channel is not None else ""
    items: list[FeedItem] = []
    for item in source:
        if _local_name(item.tag) != "item":
            continue
        title = _clean_text(_child_text(item, "title"))
        link = _clean_text(_child_text(item, "link"))
        guid = _clean_text(_child_text(item, "guid"))
        published = _format_datetime(_child_text(item, "pubDate") or _child_text(item, "updated"))
        raw_summary = _child_text(item, "description") or _child_text(item, "content")
        summary = _clean_text(raw_summary)
        author_name = _clean_text(_child_text(item, "author")) or feed_author
        item_id = guid or link or title
        if item_id:
            items.append(
                FeedItem(
                    item_id=item_id,
                    title=title or summary[:80] or link,
                    link=link,
                    published_at=published,
                    summary=summary,
                    author_name=author_name,
                    image_urls=_dedupe(_extract_image_urls(raw_summary) + _element_image_urls(item)),
                    avatar_url="",
                )
            )
    return items


def _parse_atom_entries(root: ET.Element) -> list[FeedItem]:
    feed_author = _feed_author_name(root)
    items: list[FeedItem] = []
    for entry in root.iter():
        if _local_name(entry.tag) != "entry":
            continue
        title = _clean_text(_child_text(entry, "title"))
        link = _atom_link(entry)
        entry_id = _clean_text(_child_text(entry, "id"))
        published = _format_datetime(_child_text(entry, "published") or _child_text(entry, "updated"))
        raw_summary = _child_text(entry, "summary") or _child_text(entry, "content")
        summary = _clean_text(raw_summary)
        author_name = _author_name(entry) or feed_author
        item_id = entry_id or link or title
        if item_id:
            items.append(
                FeedItem(
                    item_id=item_id,
                    title=title or summary[:80] or link,
                    link=link,
                    published_at=published,
                    summary=summary,
                    author_name=author_name,
                    image_urls=_dedupe(_extract_image_urls(raw_summary) + _element_image_urls(entry)),
                    avatar_url="",
                )
            )
    return items


def _parse_dynamic_item(item: dict[str, Any]) -> FeedItem | None:
    modules = item.get("modules") or {}
    module_author = modules.get("module_author") or {}
    module_dynamic = modules.get("module_dynamic") or {}
    major = module_dynamic.get("major") or {}
    major_type = str(major.get("type") or "").strip()
    dynamic_type = str(item.get("type") or "").strip()
    item_id = str(item.get("id_str") or item.get("id") or "").strip()
    if not item_id:
        return None

    author_name = str(module_author.get("name") or module_author.get("face") or "").strip()
    avatar_url = _normalize_image_url(str(module_author.get("face") or "").strip())
    published_at = _format_unix_ts(module_author.get("pub_ts"))
    title = _clean_text(_dynamic_title(module_dynamic, major, major_type))
    summary = _clean_text(_dynamic_summary(module_dynamic, major, major_type, dynamic_type))
    link = _dynamic_link(item, item_id)
    image_urls = _dedupe(_collect_image_urls(major))

    return FeedItem(
        item_id=item_id,
        title=title or summary[:80] or link,
        link=link,
        published_at=published_at,
        summary=summary or title,
        author_name=author_name,
        image_urls=image_urls,
        avatar_url=avatar_url,
    )


def _parse_desktop_dynamic_item(item: dict[str, Any]) -> FeedItem | None:
    item_id = str(item.get("id_str") or item.get("id") or "").strip()
    if not item_id:
        return None

    modules = item.get("modules") or []
    author_module = _desktop_module(modules, "MODULE_TYPE_AUTHOR").get("module_author") or {}
    desc_module = _desktop_module(modules, "MODULE_TYPE_DESC").get("module_desc") or {}
    dynamic_module = _desktop_module(modules, "MODULE_TYPE_DYNAMIC").get("module_dynamic") or {}
    author_user = author_module.get("user") or {}

    author_name = _clean_text(author_user.get("name") or author_module.get("name"))
    avatar_url = _normalize_image_url(str(author_user.get("face") or author_module.get("face") or "").strip())
    published_at = _clean_text(author_module.get("pub_text") or author_module.get("pub_time"))
    live_info = _dig(dynamic_module, "dyn_live_rcmd", "card_info", "live_play_info") or {}
    summary = _clean_multiline_text(
        _first_non_empty(
            desc_module.get("text"),
            _dig(dynamic_module, "dyn_common", "text"),
            _dig(dynamic_module, "additional", "common", "desc2"),
            _dig(dynamic_module, "additional", "common", "title"),
        )
    )
    title = _clean_text(summary.splitlines()[0] if summary else "")
    link = f"https://t.bilibili.com/{item_id}"
    image_urls = _dedupe(_desktop_dynamic_image_urls(dynamic_module))

    if live_info:
        title = _clean_text(live_info.get("title") or "正在直播")
        live_status = int(live_info.get("live_status") or 0)
        status_text = "正在直播" if live_status == 1 else "直播动态"
        area_name = _clean_text(live_info.get("area_name") or "")
        online = live_info.get("online")
        online_text = f"在线：{online}" if isinstance(online, (int, float)) and online > 0 else ""
        summary = "\n".join(
            part for part in (status_text, f"分区：{area_name}" if area_name else "", online_text) if part
        )
        live_link = str(live_info.get("link") or "").strip()
        if live_link.startswith("//"):
            live_link = "https:" + live_link
        if live_link.startswith("https://live.bilibili.com/"):
            link = live_link
        room_id = str(live_info.get("room_id") or live_info.get("live_id") or "").strip()
        if link.startswith("https://t.bilibili.com/") and room_id:
            link = f"https://live.bilibili.com/{room_id}"
        image_urls = _dedupe(
            _collect_image_urls(live_info.get("cover"))
            + _desktop_dynamic_image_urls(dynamic_module)
        )

    return FeedItem(
        item_id=item_id,
        title=title or summary[:80] or link,
        link=link,
        published_at=published_at,
        summary=summary or title,
        author_name=author_name,
        image_urls=image_urls,
        avatar_url=avatar_url,
    )


def _dynamic_title(module_dynamic: dict[str, Any], major: dict[str, Any], major_type: str) -> str:
    desc_text = _clean_text(_dig(module_dynamic, "desc", "text"))
    if desc_text:
        return desc_text

    if major_type == "MAJOR_TYPE_ARCHIVE":
        return _clean_text(_dig(major, "archive", "title"))
    if major_type == "MAJOR_TYPE_ARTICLE":
        return _clean_text(_dig(major, "article", "title"))
    if major_type == "MAJOR_TYPE_COMMON":
        return _clean_text(_dig(major, "common", "title"))
    if major_type == "MAJOR_TYPE_OPUS":
        return _clean_text(_first_non_empty(_dig(major, "opus", "title"), _dig(major, "opus", "summary", "text")))
    return ""


def _dynamic_summary(module_dynamic: dict[str, Any], major: dict[str, Any], major_type: str, dynamic_type: str) -> str:
    desc_text = _clean_text(_dig(module_dynamic, "desc", "text"))
    if desc_text:
        return desc_text

    if major_type == "MAJOR_TYPE_DRAW":
        return "发布了图片动态"
    if major_type == "MAJOR_TYPE_ARCHIVE":
        return _clean_text(_first_non_empty(_dig(major, "archive", "title"), _dig(major, "archive", "desc"), "发布了视频动态"))
    if major_type == "MAJOR_TYPE_ARTICLE":
        return _clean_text(_first_non_empty(_dig(major, "article", "title"), _dig(major, "article", "desc"), "发布了专栏动态"))
    if major_type == "MAJOR_TYPE_COMMON":
        return _clean_text(_first_non_empty(_dig(major, "common", "title"), _dig(major, "common", "desc"), "发布了动态"))
    if major_type == "MAJOR_TYPE_OPUS":
        return _clean_text(_first_non_empty(_dig(major, "opus", "summary", "text"), _dig(major, "opus", "title"), "发布了图文动态"))

    if dynamic_type == "DYNAMIC_TYPE_FORWARD":
        return "转发了一条动态"
    if dynamic_type == "DYNAMIC_TYPE_AV":
        return "发布了视频动态"
    if dynamic_type == "DYNAMIC_TYPE_ARTICLE":
        return "发布了专栏动态"
    if dynamic_type == "DYNAMIC_TYPE_DRAW":
        return "发布了图片动态"

    candidates = [
        _dig(major, "archive", "title"),
        _dig(major, "archive", "desc"),
        _dig(major, "article", "title"),
        _dig(major, "article", "desc"),
        _dig(major, "common", "title"),
        _dig(major, "common", "desc"),
        _dig(major, "opus", "title"),
        _dig(major, "opus", "summary", "text"),
    ]
    parts = [_clean_text(value) for value in candidates if _clean_text(value)]
    if parts:
        return "\n".join(_dedupe(parts))
    return "发布了动态"


def _dynamic_link(item: dict[str, Any], item_id: str) -> str:
    candidates = [
        _dig(item, "basic", "jump_url"),
        _dig(item, "basic", "web_rid"),
        _dig(item, "jump_url"),
    ]
    for value in candidates:
        raw = str(value or "").strip()
        if not raw:
            continue
        if raw.startswith("//"):
            return "https:" + raw
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
    return f"https://t.bilibili.com/{item_id}"


def _desktop_module(modules: list[dict[str, Any]], module_type: str) -> dict[str, Any]:
    for module in modules:
        if str(module.get("module_type") or "").strip() == module_type:
            return module
    return {}


def _dynamic_is_pinned(item: dict[str, Any]) -> bool:
    modules = item.get("modules") or {}
    module_tag = modules.get("module_tag") or {}
    tag_text = _clean_text(module_tag.get("text") or module_tag.get("tag") or "")
    return tag_text == "置顶"


def _desktop_dynamic_image_urls(dynamic_module: dict[str, Any]) -> list[str]:
    candidates = [
        _dig(dynamic_module, "dyn_draw", "items"),
        _dig(dynamic_module, "dyn_opus", "pics"),
        _dig(dynamic_module, "cover"),
        _dig(dynamic_module, "dyn_archive", "cover"),
        _dig(dynamic_module, "dyn_article", "covers"),
        _dig(dynamic_module, "dyn_article", "cover"),
        _dig(dynamic_module, "dyn_common", "cover"),
        _dig(dynamic_module, "dyn_live_rcmd", "card_info", "live_play_info", "cover"),
    ]
    results: list[str] = []
    for candidate in candidates:
        results.extend(_collect_image_urls(candidate))
    return results


def _collect_image_urls(value: Any) -> list[str]:
    results: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            key_name = str(key).lower()
            if key_name in {"url", "src", "cover"} and isinstance(inner, str):
                normalized = _normalize_image_url(inner)
                if normalized:
                    results.append(normalized)
                    continue
            results.extend(_collect_image_urls(inner))
    elif isinstance(value, list):
        for inner in value:
            results.extend(_collect_image_urls(inner))
    elif isinstance(value, str):
        normalized = _normalize_image_url(value)
        if normalized:
            results.append(normalized)
    return results


def _normalize_image_url(value: str) -> str:
    raw = unescape(str(value or "")).strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if not raw.startswith(("http://", "https://")):
        return ""
    if not (
        _IMAGE_URL_RE.search(raw)
        or "hdslb.com" in raw
        or "biliimg.com" in raw
    ):
        return ""
    return raw


def _extract_image_urls(value: str) -> list[str]:
    text = unescape(str(value or ""))
    return [normalized for raw in _IMG_SRC_RE.findall(text) if (normalized := _normalize_image_url(raw))]


def _element_image_urls(element: ET.Element) -> list[str]:
    urls: list[str] = []
    for child in element.iter():
        name = _local_name(child.tag)
        if name in {"content", "thumbnail", "enclosure"}:
            for key in ("url", "href", "src"):
                value = child.attrib.get(key)
                normalized = _normalize_image_url(value or "")
                if normalized:
                    urls.append(normalized)
    return urls


def _first_child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _child_text(element: ET.Element | None, name: str) -> str:
    if element is None:
        return ""
    for child in element:
        if _local_name(child.tag) == name:
            return "".join(child.itertext())
    return ""


def _author_name(element: ET.Element | None) -> str:
    author = _first_child(element, "author")
    return _clean_text(_child_text(author, "name")) if author is not None else ""


def _feed_author_name(root: ET.Element) -> str:
    channel = _first_child(root, "channel")
    if channel is not None:
        title = _clean_text(_child_text(channel, "title"))
        if title:
            return title
    return _author_name(root)


def _atom_link(entry: ET.Element) -> str:
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        rel = (child.attrib.get("rel") or "").strip().lower()
        if href and rel in {"", "alternate"}:
            return href.strip()
    return _clean_text(_child_text(entry, "link"))


def _dig(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _collect_text(value: Any) -> str:
    results: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for inner in node.values():
                walk(inner)
            return
        if isinstance(node, list):
            for inner in node:
                walk(inner)
            return
        if isinstance(node, str):
            cleaned = _clean_text(node)
            if cleaned:
                results.append(cleaned)

    walk(value)
    return "\n".join(_dedupe(results[:8]))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = unescape(str(value or "")).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _clean_text(value: str) -> str:
    text = unescape(str(value or ""))
    text = _TAG_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _clean_multiline_text(value: str) -> str:
    text = unescape(str(value or ""))
    text = _TAG_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    result: list[str] = []
    blank_pending = False
    for line in lines:
        if not line:
            blank_pending = bool(result)
            continue
        if blank_pending:
            result.append("")
            blank_pending = False
        result.append(line)
    return "\n".join(result).strip()


def _format_datetime(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = parsedate_to_datetime(raw)
        return parsed.astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone().isoformat(timespec="seconds")
    except ValueError:
        return raw


def _format_unix_ts(value: Any) -> str:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")
