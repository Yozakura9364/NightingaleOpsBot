from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
import re
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse, unquote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class FeedItem:
    item_id: str
    title: str
    link: str
    published_at: str
    summary: str
    author_name: str
    author_avatar_url: str
    image_urls: list[str]


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_IMG_SRC_RE = re.compile(r"""<img\b[^>]*\bsrc=["']([^"']+)["']""", re.I)


def normalize_uid(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("请提供微博 UID 或用户链接。")
    match = re.search(r"/u/(\d{5,20})", raw)
    if match:
        return match.group(1)
    match = re.search(r"weibo\.com/(\d{5,20})(?:[/?#]|$)", raw, flags=re.I)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d{5,20}", raw):
        return raw
    raise ValueError("微博 UID 格式不正确，请使用纯数字 UID 或 https://weibo.com/u/1234567890 这样的链接。")


def fetch_user_feed(base_url: str, uid: str, *, timeout: float = 30.0) -> list[FeedItem]:
    normalized = normalize_uid(uid)
    url = _feed_url(base_url, normalized)
    request = Request(url, headers={"User-Agent": "NightingaleOpsBot-WeiboFeed/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read(2_000_000)
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as error:
        body = error.read(20_000).decode("utf-8", errors="replace")
        raise RuntimeError(_http_error_message(error.code, body)) from error
    except URLError as error:
        raise RuntimeError(f"连接 weibo-rss 失败：{error.reason}") from error

    if status >= 400:
        raise RuntimeError(f"weibo-rss 返回 HTTP {status}")

    text = body.decode(_detect_charset(content_type) or "utf-8", errors="replace").strip()
    if not text:
        return []
    if text.lower().startswith("<!doctype html") or "<html" in text[:200].lower():
        raise RuntimeError(_html_error_message(text))

    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise RuntimeError(f"weibo-rss 返回内容不是有效 RSS/XML：{error}") from error

    return _parse_rss_items(root)


def _feed_url(base_url: str, uid: str) -> str:
    return f"{str(base_url or '').rstrip('/')}/rss/user/{quote(uid)}"


def _detect_charset(content_type: str) -> str | None:
    match = re.search(r"charset=([^;\s]+)", content_type or "", flags=re.I)
    return match.group(1).strip("\"'") if match else None


def _http_error_message(status: int, body: str) -> str:
    detail = _html_error_message(body) if body else ""
    return f"weibo-rss 返回 HTTP {status}" + (f"：{detail}" if detail else "")


def _html_error_message(text: str) -> str:
    title = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    return _clean_text(title.group(1)) if title else "weibo-rss 返回了 HTML 错误页"


def _parse_rss_items(root: ET.Element) -> list[FeedItem]:
    channel = _first_child(root, "channel")
    source: Iterable[ET.Element] = channel if channel is not None else root
    author_name = _clean_text(_child_text(channel, "title")) if channel is not None else ""
    author_name = re.sub(r"\s*的微博\s*$", "", author_name)
    items: list[FeedItem] = []
    for item in source:
        if _local_name(item.tag) != "item":
            continue
        title = _clean_text(_child_text(item, "title"))
        link = _clean_text(_child_text(item, "link"))
        guid = _clean_text(_child_text(item, "guid"))
        published = _format_datetime(_child_text(item, "pubDate") or _child_text(item, "updated"))
        raw_summary = _child_text(item, "description") or _child_text(item, "content")
        summary = _clean_summary(raw_summary)
        title, summary = _split_summary_title(title, summary)
        author_avatar_url = _normalize_image_url(
            _child_text(item, "weibo_avatar") or _child_text(channel, "weibo_avatar")
        )
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
                    author_avatar_url=author_avatar_url,
                    image_urls=_dedupe(_extract_image_urls(raw_summary) + _element_image_urls(item)),
                )
            )
    return items


def _first_child(element: ET.Element, name: str) -> ET.Element | None:
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


def _extract_image_urls(value: str) -> list[str]:
    text = unescape(str(value or ""))
    return [_normalize_image_url(url) for url in _IMG_SRC_RE.findall(text) if _is_image_url(url)]


def _element_image_urls(element: ET.Element) -> list[str]:
    urls: list[str] = []
    for child in element.iter():
        name = _local_name(child.tag)
        if name in {"content", "thumbnail", "enclosure"}:
            for key in ("url", "href", "src"):
                value = child.attrib.get(key)
                if value and _is_image_url(value):
                    urls.append(_normalize_image_url(value))
    return urls


def _is_image_url(value: str) -> bool:
    url = unescape(str(value or "")).strip()
    if not url.startswith(("http://", "https://")):
        return False
    return bool(
        re.search(r"\.(?:jpg|jpeg|png|gif|webp)(?:[?:#]|$)", url, flags=re.I)
        or "sinaimg.cn" in url
        or "image.baidu.com/search/down?url=" in url
    )


def _normalize_image_url(value: str) -> str:
    url = unescape(str(value or "")).strip()
    parsed = urlparse(url)
    if parsed.netloc.lower() == "image.baidu.com" and parsed.path.startswith("/search/down"):
        inner = parse_qs(parsed.query).get("url", [])
        if inner and inner[0]:
            return unquote(inner[0]).strip()
    return url


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


_BLOCK_END_RE = re.compile(r"<\s*/\s*(?:p|div|li|blockquote|h[1-6]|tr)\s*>", re.I)
_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>", re.I)


def _clean_summary(value: str) -> str:
    text = unescape(str(value or ""))
    text = _BREAK_RE.sub("\n", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_summary_title(title: str, summary: str) -> tuple[str, str]:
    current_title = str(title or "").strip()
    body = str(summary or "").strip()
    if not body:
        return current_title, body

    first_line, separator, remainder = body.partition("\n")
    inferred_title = first_line.strip()
    if not inferred_title:
        return current_title, body

    # weibo-rss truncates the RSS title, while the first body paragraph keeps it whole.
    if not current_title or inferred_title.startswith(current_title.strip()) or len(inferred_title) > len(current_title):
        current_title = inferred_title
    if body.startswith(inferred_title):
        body = remainder.lstrip() if separator else ""
    return current_title, body


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
