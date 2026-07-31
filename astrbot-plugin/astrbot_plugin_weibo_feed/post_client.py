from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
import json
from math import ceil
import re
from time import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .feed_client import FeedItem


_WEIBO_HOSTS = ("weibo.com", "weibo.cn")
_SHORT_LINK_HOSTS = {"t.cn", "mapp.api.weibo.cn"}
_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>", re.I)
_BLOCK_END_RE = re.compile(r"<\s*/\s*(?:p|div|li|blockquote|h[1-6]|tr)\s*>", re.I)


def is_weibo_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").lower().strip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    return host == "t.cn" or any(host == root or host.endswith("." + root) for root in _WEIBO_HOSTS)


def fetch_weibo_post(
    url: str,
    *,
    timeout: float = 15.0,
    proxy_url: str = "",
) -> tuple[str, FeedItem]:
    source_url = str(url or "").strip()
    if not is_weibo_url(source_url):
        raise ValueError("不是受支持的微博链接。")

    opener = _build_opener(proxy_url)
    resolved_url = _resolve_share_url(opener, source_url, timeout, proxy_url)
    post_id = _extract_post_id(resolved_url)
    if not post_id:
        raise ValueError("微博链接中没有可识别的动态 ID。")

    api_url = f"https://m.weibo.cn/statuses/show?id={post_id}&_={int(time() * 1000)}"
    request = Request(
        api_url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://m.weibo.cn",
            "Referer": f"https://m.weibo.cn/detail/{post_id}",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "MWeibo-Pwa": "1",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            content_type = str(response.headers.get("Content-Type", ""))
            body = response.read(2_000_000)
    except HTTPError as error:
        if error.code in {403, 418}:
            raise RuntimeError(f"微博接口触发访问限制（HTTP {error.code}）。") from error
        raise RuntimeError(f"微博接口返回 HTTP {error.code}。") from error
    except URLError as error:
        raise RuntimeError(f"连接微博接口失败：{error.reason}") from error

    if status != 200:
        raise RuntimeError(f"微博接口返回 HTTP {status}。")
    if "json" not in content_type.lower():
        raise RuntimeError(f"微博接口返回类型异常：{content_type or 'unknown'}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("微博接口返回内容不是有效 JSON。") from error

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        message = str(payload.get("msg") or payload.get("message") or "动态数据为空") if isinstance(payload, dict) else "动态数据为空"
        raise RuntimeError(f"微博接口请求失败：{message}")
    return post_id, _feed_item_from_status(data, fallback_id=post_id)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener(proxy_url: str, *, follow_redirects: bool = True):
    proxy = str(proxy_url or "").strip()
    handlers = [ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    if not follow_redirects:
        handlers.append(_NoRedirectHandler())
    return build_opener(*handlers)


def _resolve_share_url(opener, source_url: str, timeout: float, proxy_url: str) -> str:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    if host not in _SHORT_LINK_HOSTS and _extract_post_id(source_url):
        return source_url

    if host in _SHORT_LINK_HOSTS:
        first_redirect = _first_redirect_url(source_url, timeout, proxy_url)
        if _extract_post_id(first_redirect):
            return first_redirect
        source_url = first_redirect

    request = Request(
        source_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36",
            "Referer": "https://weibo.com/",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            resolved = str(response.geturl() or source_url)
            response.read(64_000)
    except HTTPError as error:
        resolved = str(error.geturl() or "")
        if not resolved or resolved == source_url:
            raise RuntimeError(f"微博短链解析返回 HTTP {error.code}。") from error
    except URLError as error:
        raise RuntimeError(f"微博短链解析失败：{error.reason}") from error

    if not is_weibo_url(resolved) or (urlparse(resolved).hostname or "").lower() == "t.cn":
        raise ValueError("微博短链没有跳转到可识别的微博动态。")
    return resolved


def _first_redirect_url(source_url: str, timeout: float, proxy_url: str) -> str:
    opener = _build_opener(proxy_url, follow_redirects=False)
    request = Request(
        source_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36",
            "Referer": "https://weibo.com/",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            location = str(response.headers.get("Location") or "").strip()
            return urljoin(source_url, location) if location else str(response.geturl() or source_url)
    except HTTPError as error:
        location = str(error.headers.get("Location") or "").strip()
        if 300 <= error.code < 400 and location:
            return urljoin(source_url, location)
        raise RuntimeError(f"微博短链解析返回 HTTP {error.code}。") from error
    except URLError as error:
        raise RuntimeError(f"微博短链解析失败：{error.reason}") from error


def _extract_post_id(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)

    if host.endswith("weibo.com") and parsed.path.startswith("/tv/show/"):
        mid = _first_query_value(query, "mid")
        if mid.isdigit():
            return _mid_to_bid(mid)

    for key in ("weibo_id", "id"):
        candidate = _first_query_value(query, key)
        if re.fullmatch(r"[0-9A-Za-z]+", candidate):
            return candidate

    path = parsed.path.strip("/")
    patterns = (
        r"(?:status|detail)/(?:[^/]+/)?(?P<id>[0-9A-Za-z]+)",
        r"(?:u/)?\d{5,20}/(?P<id>[0-9A-Za-z]+)",
        r"share/(?P<id>\d+)\.html",
    )
    for pattern in patterns:
        match = re.search(pattern, path, flags=re.I)
        if match:
            return match.group("id")
    return ""


def _first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    return str(values[0] if values else "").strip()


def _mid_to_bid(mid: str) -> str:
    reversed_mid = str(mid)[::-1]
    chunks: list[str] = []
    size = ceil(len(reversed_mid) / 7)
    for index in range(size):
        chunk = reversed_mid[index * 7 : (index + 1) * 7][::-1]
        encoded = _base62_encode(int(chunk))
        if index < size - 1:
            encoded = encoded.rjust(4, "0")
        chunks.append(encoded)
    return "".join(reversed(chunks))


def _base62_encode(number: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if number == 0:
        return "0"
    result = ""
    while number > 0:
        result = alphabet[number % 62] + result
        number //= 62
    return result


def _feed_item_from_status(data: dict[str, Any], *, fallback_id: str) -> FeedItem:
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    author_name = str(user.get("screen_name") or "").strip()
    author_avatar_url = str(user.get("avatar_hd") or user.get("profile_image_url") or "").strip()
    body = _clean_weibo_text(data.get("text"))
    title, summary = _split_title_and_summary(body)

    retweeted = data.get("retweeted_status")
    image_urls = _status_image_urls(data)
    if isinstance(retweeted, dict):
        repost_user = retweeted.get("user") if isinstance(retweeted.get("user"), dict) else {}
        repost_author = str(repost_user.get("screen_name") or "").strip()
        repost_body = _clean_weibo_text(retweeted.get("text"))
        if repost_body:
            label = f"转发 @{repost_author}" if repost_author else "转发微博"
            summary = "\n\n".join(part for part in (summary, f"{label}：{repost_body}") if part)
        image_urls.extend(_status_image_urls(retweeted))

    if not image_urls:
        cover = _page_cover_url(data)
        if cover:
            image_urls.append(cover)

    uid = str(user.get("id") or user.get("idstr") or "").strip()
    bid = str(data.get("bid") or fallback_id).strip()
    canonical_url = f"https://weibo.com/{uid}/{bid}" if uid and bid else f"https://m.weibo.cn/detail/{fallback_id}"
    item_id = str(data.get("idstr") or data.get("id") or bid or fallback_id).strip()

    return FeedItem(
        item_id=item_id,
        title=title or summary[:80] or author_name or "微博",
        link=canonical_url,
        published_at=_format_datetime(data.get("created_at")),
        summary=summary,
        author_name=author_name,
        author_avatar_url=author_avatar_url,
        image_urls=_dedupe(image_urls),
    )


def _clean_weibo_text(value: Any) -> str:
    text = unescape(str(value or ""))
    text = _BREAK_RE.sub("\n", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = text.replace("\u200b", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split_title_and_summary(body: str) -> tuple[str, str]:
    value = str(body or "").strip()
    if not value:
        return "", ""
    first_line, separator, remainder = value.partition("\n")
    return first_line.strip(), remainder.lstrip() if separator else ""


def _status_image_urls(data: dict[str, Any]) -> list[str]:
    result: list[str] = []
    pics = data.get("pics")
    if not isinstance(pics, list):
        return result
    for pic in pics:
        if not isinstance(pic, dict):
            continue
        large = pic.get("large") if isinstance(pic.get("large"), dict) else {}
        value = str(large.get("url") or pic.get("url") or "").strip()
        if value.startswith(("http://", "https://")):
            result.append(value)
    return result


def _page_cover_url(data: dict[str, Any]) -> str:
    page_info = data.get("page_info") if isinstance(data.get("page_info"), dict) else {}
    page_pic = page_info.get("page_pic") if isinstance(page_info.get("page_pic"), dict) else {}
    return str(page_pic.get("url") or "").strip()


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _format_datetime(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return raw
