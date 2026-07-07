from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
import re
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class FeedItem:
    item_id: str
    title: str
    link: str
    published_at: str
    summary: str
    image_urls: list[str]


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_IMG_SRC_RE = re.compile(r"""<img\b[^>]*\bsrc=["']([^"']+)["']""", re.I)


def normalize_handle(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if raw.startswith("https://x.com/") or raw.startswith("https://twitter.com/"):
        raw = raw.rstrip("/").split("/")[-1]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", raw):
        raise ValueError("X 账号格式不正确，请使用类似 @FF_XIV_EN 的 handle。")
    return raw.lower()


def fetch_user_feed(base_url: str, handle: str, *, timeout: float = 30.0) -> list[FeedItem]:
    normalized = normalize_handle(handle)
    url = _feed_url(base_url, normalized)
    request = Request(url, headers={"User-Agent": "NightingaleOpsBot-XFeed/0.1"})
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


def _feed_url(base_url: str, handle: str) -> str:
    return f"{str(base_url or '').rstrip('/')}/twitter/user/{quote(handle)}"


def _detect_charset(content_type: str) -> str | None:
    match = re.search(r"charset=([^;\s]+)", content_type or "", flags=re.I)
    return match.group(1).strip("\"'") if match else None


def _http_error_message(status: int, body: str) -> str:
    detail = _html_error_message(body) if body else ""
    return f"RSSHub 返回 HTTP {status}" + (f"：{detail}" if detail else "")


def _html_error_message(text: str) -> str:
    match = re.search(
        r"Error Message:\s*<br\s*/?>\s*<code[^>]*>(.*?)</code>",
        text,
        flags=re.I | re.S,
    )
    if match:
        return _clean_text(match.group(1))
    match = re.search(r"(ConfigNotFoundError:[^<\n]+)", text, flags=re.I)
    if match:
        return _clean_text(match.group(1))
    title = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    return _clean_text(title.group(1)) if title else "RSSHub 返回了 HTML 错误页"


def _parse_rss_items(root: ET.Element) -> list[FeedItem]:
    channel = _first_child(root, "channel")
    source: Iterable[ET.Element] = channel if channel is not None else root
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
        item_id = guid or link or title
        if item_id:
            items.append(
                FeedItem(
                    item_id=item_id,
                    title=title or summary[:80] or link,
                    link=link,
                    published_at=published,
                    summary=summary,
                    image_urls=_dedupe(_extract_image_urls(raw_summary) + _element_image_urls(item)),
                )
            )
    return items


def _parse_atom_entries(root: ET.Element) -> list[FeedItem]:
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
        item_id = entry_id or link or title
        if item_id:
            items.append(
                FeedItem(
                    item_id=item_id,
                    title=title or summary[:80] or link,
                    link=link,
                    published_at=published,
                    summary=summary,
                    image_urls=_dedupe(_extract_image_urls(raw_summary) + _element_image_urls(entry)),
                )
            )
    return items


def _first_child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name:
            return "".join(child.itertext())
    return ""


def _atom_link(entry: ET.Element) -> str:
    for child in entry:
        if _local_name(child.tag) == "link":
            href = child.attrib.get("href")
            if href:
                return href.strip()
    return _clean_text(_child_text(entry, "link"))


def _extract_image_urls(value: str) -> list[str]:
    text = unescape(str(value or ""))
    return [url.strip() for url in _IMG_SRC_RE.findall(text) if _is_image_url(url)]


def _element_image_urls(element: ET.Element) -> list[str]:
    urls: list[str] = []
    for child in element.iter():
        name = _local_name(child.tag)
        if name in {"content", "thumbnail", "enclosure"}:
            for key in ("url", "href"):
                value = child.attrib.get(key)
                if value and _is_image_url(value):
                    urls.append(unescape(value.strip()))
    return urls


def _is_image_url(value: str) -> bool:
    url = unescape(str(value or "")).strip()
    if not url.startswith(("http://", "https://")):
        return False
    return bool(
        re.search(r"\.(?:jpg|jpeg|png|gif|webp)(?:[?:#]|$)", url, flags=re.I)
        or "pbs.twimg.com/media/" in url
    )


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
