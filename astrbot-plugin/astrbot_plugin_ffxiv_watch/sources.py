from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
import html
import json
import re
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, build_opener
import xml.etree.ElementTree as ET


USER_AGENT = "Mozilla/5.0 NightingaleOpsBot-FFXIVWatch/0.1"
DEFAULT_RSSHUB_BASE_URL = "http://rsshub:1200"


@dataclass(frozen=True)
class SourceDefinition:
    id: str
    kind: str
    region: str
    label: str
    url: str


@dataclass(frozen=True)
class WatchItem:
    source_id: str
    source_label: str
    kind: str
    region: str
    item_id: str
    title: str
    url: str
    published_at: str = ""
    category: str = ""
    summary: str = ""
    image: str = ""
    price: str = ""
    currency: str = ""
    event_key: str = ""

    def stable_key(self) -> str:
        if self.event_key:
            return self.event_key
        item_id = self.item_id or _hash_text(self.url or self.title)
        return f"{self.kind}:{self.source_id}:{item_id}"

    def payload(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "source_label": self.source_label,
            "kind": self.kind,
            "region": self.region,
            "item_id": self.item_id,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "category": self.category,
            "summary": self.summary,
            "image": self.image,
            "price": self.price,
            "currency": self.currency,
        }


SOURCES: dict[str, SourceDefinition] = {
    "cn-news": SourceDefinition(
        id="cn-news",
        kind="news",
        region="国服",
        label="国服官网新闻",
        url="/ff14/zh/news",
    ),
    "cn-notice": SourceDefinition(
        id="cn-notice",
        kind="news",
        region="国服",
        label="国服官网公告",
        url="/ff14/zh/announce",
    ),
    "jp-news": SourceDefinition(
        id="jp-news",
        kind="news",
        region="日服",
        label="Lodestone",
        url="/ff14/global/jp/all",
    ),
    "cn-store": SourceDefinition(
        id="cn-store",
        kind="store",
        region="国服",
        label="盛趣商城",
        url="https://qu.sdo.com/product-detail/0d527e640bd3ada51565",
    ),
    "tw-store": SourceDefinition(
        id="tw-store",
        kind="store",
        region="台服",
        label="水晶商城",
        url="https://www.ffxiv.com.tw/web/store/product_detail.aspx?id=F0068_251120152555",
    ),
    "jp-store": SourceDefinition(
        id="jp-store",
        kind="store",
        region="日服",
        label="Online Store",
        url="https://store.finalfantasyxiv.com/ffxivstore/ja-jp/product/392",
    ),
}


def list_sources() -> list[SourceDefinition]:
    return list(SOURCES.values())


def source_ids_for_kind(kind: str) -> list[str]:
    return [source.id for source in SOURCES.values() if source.kind == kind]


def fetch_source(
    source_id: str,
    timeout_seconds: int = 20,
    rsshub_base_url: str = DEFAULT_RSSHUB_BASE_URL,
) -> list[WatchItem]:
    source = SOURCES.get(source_id)
    if not source:
        raise ValueError(f"未知数据源：{source_id}")
    if source_id in {"cn-news", "cn-notice", "jp-news"}:
        return _fetch_rsshub_news(source, timeout_seconds, rsshub_base_url=rsshub_base_url)
    if source_id == "cn-store":
        return _fetch_cn_store(source, timeout_seconds)
    if source_id in {"tw-store", "jp-store"}:
        return _fetch_generic_product_page(source, timeout_seconds)
    raise ValueError(f"未实现的数据源：{source_id}")


def _fetch_rsshub_news(
    source: SourceDefinition,
    timeout_seconds: int,
    *,
    rsshub_base_url: str,
) -> list[WatchItem]:
    feed_url = _rsshub_url(rsshub_base_url, source.url)
    text = _http_get(feed_url, timeout_seconds=timeout_seconds)
    root = _parse_xml(text)
    items: list[WatchItem] = []
    for row in _parse_feed_items(root):
        url = row.get("link", "")
        title = row.get("title", "")
        if not title and row.get("summary"):
            title = row["summary"][:80]
        if not url and not title:
            continue
        item_id = row.get("guid") or url or title
        items.append(
            WatchItem(
                source_id=source.id,
                source_label=source.label,
                kind=source.kind,
                region=source.region,
                item_id=_hash_text(item_id),
                title=title,
                url=url,
                published_at=row.get("published_at", ""),
                category=row.get("category", ""),
                summary=row.get("summary", "")[:240],
                image=row.get("image", ""),
            )
        )
    return _dedupe_items(items)


def _rsshub_url(base_url: str, route: str) -> str:
    base = str(base_url or DEFAULT_RSSHUB_BASE_URL).rstrip("/")
    path = str(route or "").strip()
    if path.startswith(("http://", "https://")):
        return path
    return f"{base}/{path.lstrip('/')}"


def _parse_xml(text: str) -> ET.Element:
    value = str(text or "").strip()
    if not value:
        raise RuntimeError("RSSHub 返回空内容")
    if value.lower().startswith("<!doctype html") or "<html" in value[:300].lower():
        raise RuntimeError("RSSHub 返回 HTML 错误页")
    try:
        return ET.fromstring(value)
    except ET.ParseError as error:
        raise RuntimeError(f"RSSHub 返回内容不是有效 RSS/XML：{error}") from error


def _parse_feed_items(root: ET.Element) -> list[dict[str, str]]:
    rss_items = _parse_rss_items(root)
    return rss_items if rss_items else _parse_atom_entries(root)


def _parse_rss_items(root: ET.Element) -> list[dict[str, str]]:
    channel = _first_child(root, "channel")
    source = list(channel) if channel is not None else list(root)
    items: list[dict[str, str]] = []
    for item in source:
        if _local_name(item.tag) != "item":
            continue
        raw_summary = _child_text(item, "description") or _child_text(item, "content")
        link = _clean_text(_child_text(item, "link"))
        guid = _clean_text(_child_text(item, "guid"))
        items.append(
            {
                "guid": guid,
                "title": _clean_text(_child_text(item, "title")),
                "link": link,
                "published_at": _format_datetime(_child_text(item, "pubDate") or _child_text(item, "updated")),
                "category": _clean_text(_child_text(item, "category")),
                "summary": _clean_text(raw_summary),
                "image": _first_image_url(raw_summary, item),
            }
        )
    return items


def _parse_atom_entries(root: ET.Element) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for entry in root.iter():
        if _local_name(entry.tag) != "entry":
            continue
        raw_summary = _child_text(entry, "summary") or _child_text(entry, "content")
        link = _atom_link(entry)
        entry_id = _clean_text(_child_text(entry, "id"))
        items.append(
            {
                "guid": entry_id,
                "title": _clean_text(_child_text(entry, "title")),
                "link": link,
                "published_at": _format_datetime(_child_text(entry, "published") or _child_text(entry, "updated")),
                "category": _clean_text(_child_text(entry, "category")),
                "summary": _clean_text(raw_summary),
                "image": _first_image_url(raw_summary, entry),
            }
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


def _first_image_url(raw_summary: str, element: ET.Element) -> str:
    text = html.unescape(str(raw_summary or ""))
    match = re.search(r"""<img\b[^>]*\bsrc=["']([^"']+)["']""", text, re.I)
    if match and _is_image_url(match.group(1)):
        return html.unescape(match.group(1)).strip()
    for child in element.iter():
        name = _local_name(child.tag)
        if name in {"content", "thumbnail", "enclosure"}:
            for key in ("url", "href"):
                value = child.attrib.get(key)
                if value and _is_image_url(value):
                    return html.unescape(value.strip())
    return ""


def _is_image_url(value: str) -> bool:
    url = html.unescape(str(value or "")).strip()
    if not url.startswith(("http://", "https://")):
        return False
    return bool(re.search(r"\.(?:jpg|jpeg|png|gif|webp)(?:[?#]|$)", url, flags=re.I))


def _local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1].lower()


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


def _fetch_tw_news(source: SourceDefinition, timeout_seconds: int) -> list[WatchItem]:
    page = _http_get(source.url, timeout_seconds=timeout_seconds)
    items: list[WatchItem] = []
    anchors = _extract_anchors(
        page,
        source.url,
        include_pattern=re.compile(r"/web/news/news_content\.aspx\?id=", re.I),
        limit=20,
    )
    for url, title in anchors:
        if _is_tw_static_news_title(title):
            continue
        item_id = _query_value(url, "id") or _hash_text(url)
        items.append(
            WatchItem(
                source_id=source.id,
                source_label=source.label,
                kind=source.kind,
                region=source.region,
                item_id=item_id,
                title=title,
                url=url,
            )
        )
    items.extend(_extract_tw_structured_news(page, source))
    return _dedupe_items(items)


def _extract_tw_structured_news(page: str, source: SourceDefinition) -> list[WatchItem]:
    items: list[WatchItem] = []
    for match in re.finditer(
        r'<div class="item">([\s\S]*?)(?=<div class="item">|<div class="page|</section|$)',
        page,
        re.I,
    ):
        block = match.group(1)
        title_match = re.search(
            r'<div class="title">[\s\S]*?<a href="([^"]+)">([\s\S]*?)</a>',
            block,
            re.I,
        )
        if not title_match:
            continue
        title = _strip_tags(title_match.group(2))
        if _is_tw_static_news_title(title):
            continue
        url = urljoin(source.url, html.unescape(title_match.group(1)))
        item_id = _query_value(url, "id") or _hash_text(url)
        date_match = re.search(r'<div class="publish_date">([^<]+)</div>', block, re.I)
        type_match = re.search(r'<div class="type ([^"]+)"', block, re.I)
        items.append(
            WatchItem(
                source_id=source.id,
                source_label=source.label,
                kind=source.kind,
                region=source.region,
                item_id=item_id,
                title=title,
                url=url,
                published_at=_clean_text(date_match.group(1) if date_match else ""),
                category=_tw_category_label(type_match.group(1) if type_match else ""),
            )
        )
    return items


def _is_tw_static_news_title(title: str) -> bool:
    value = _clean_text(title)
    if not value:
        return True
    static_titles = {
        "冒險指南",
        "一步步帶您踏入FINAL FANTASY XIV 繁體中文版的世界！",
        "主程式下載與安裝、開卡教學",
        "跨界傳送功能教學",
    }
    return value in static_titles


def _tw_category_label(value: str) -> str:
    mapping = {
        "topics": "活動",
        "update": "更新",
        "maintain": "維護",
        "other": "其他",
    }
    return mapping.get(str(value or "").strip(), "")


def _fetch_cn_store(source: SourceDefinition, timeout_seconds: int) -> list[WatchItem]:
    sku_id = _path_match(source.url, r"product-detail/([^/?#]+)") or "0d527e640bd3ada51565"
    merchant_id = _fetch_cn_store_merchant_id(sku_id, timeout_seconds)
    api_url = f"https://sqmallservice.u.sdo.com/api/ps/product/allInOne?skuId={sku_id}"
    payload = json.loads(
        _http_get(
            api_url,
            timeout_seconds=timeout_seconds,
            headers=_cn_store_headers(merchant_id),
        )
    )
    data = payload.get("data") or {}
    sku = ((data.get("priceInfo") or {}).get("sku") or {}) if isinstance(data, dict) else {}
    product = ((data.get("productBasicInfo") or {}).get("product") or {}) if isinstance(data, dict) else {}
    title = _clean_text(str(sku.get("productName") or product.get("productName") or ""))
    if not title:
        return []
    price = str(sku.get("netPrice") or sku.get("memberPrice") or sku.get("originalPrice") or "")
    currency = ""
    if isinstance(sku.get("currency"), dict):
        currency = str(
            sku["currency"].get("shortName")
            or sku["currency"].get("baseUnit")
            or sku["currency"].get("fullName")
            or ""
        )
    image = str(sku.get("picUrl") or product.get("picUrl") or "")
    summary = _strip_tags(str(sku.get("description") or product.get("description") or product.get("productContent") or ""))
    signature = _hash_text(
        "|".join(
            [
                title,
                price,
                currency,
                str(sku.get("saleable") or ""),
                str(sku.get("publishStatus") or product.get("publishStatus") or ""),
            ]
        )
    )
    return [
        WatchItem(
            source_id=source.id,
            source_label=source.label,
            kind=source.kind,
            region=source.region,
            item_id=str(sku.get("skuId") or sku_id),
            title=title,
            url=source.url,
            summary=summary[:240],
            image=urljoin(source.url, image) if image else "",
            price=price,
            currency=currency,
            event_key=f"store-change:{source.id}:{sku_id}:{signature}",
        )
    ]


def _fetch_cn_store_merchant_id(sku_id: str, timeout_seconds: int) -> str:
    api_url = f"https://sqmallservice.u.sdo.com/api/cs/merchant/getBySkuId?skuId={sku_id}"
    try:
        payload = json.loads(
            _http_get(
                api_url,
                timeout_seconds=timeout_seconds,
                headers=_cn_store_headers("1"),
            )
        )
        return str((payload.get("data") or {}).get("merchantId") or "1")
    except Exception:
        return "1"


def _fetch_generic_product_page(source: SourceDefinition, timeout_seconds: int) -> list[WatchItem]:
    page = _http_get(source.url, timeout_seconds=timeout_seconds)
    title = _extract_product_title(page) or _normalize_page_title(_extract_meta(page, "og:title") or _extract_title(page))
    title = _clean_text(title)
    if not title:
        return []
    parsed = urlparse(source.url)
    product_id = (
        _path_match(source.url, r"/product/(\d+)")
        or _path_match(source.url, r"/detail/(\d+)")
        or _query_value(source.url, "id")
        or _hash_text(source.url)
    )
    price = _find_price(page)
    image = _extract_meta(page, "og:image")
    summary = _strip_tags(_extract_meta(page, "og:description") or _extract_meta(page, "description"))[:240]
    signature = _hash_text("|".join([title, price, image, summary]))
    return [
        WatchItem(
            source_id=source.id,
            source_label=source.label,
            kind=source.kind,
            region=source.region,
            item_id=product_id,
            title=title,
            url=source.url,
            summary=summary,
            image=urljoin(source.url, image) if image else "",
            price=price,
            event_key=f"store-change:{source.id}:{product_id}:{signature}",
        )
    ]


def _http_get(url: str, *, timeout_seconds: int, headers: dict[str, str] | None = None) -> str:
    request_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "User-Agent": USER_AGENT,
    }
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    opener = build_opener()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")
            charset = _charset_from_content_type(content_type)
            return data.decode(charset, errors="replace")
    except HTTPError as error:
        raise RuntimeError(f"请求失败：HTTP {error.code}") from error
    except socket.timeout as error:
        raise RuntimeError(f"请求超时：{url}") from error
    except URLError as error:
        raise RuntimeError(f"请求失败：{error.reason}") from error


def _cn_store_headers(merchant_id: str) -> dict[str, str]:
    return {
        "Accept": "application/json,text/plain,*/*",
        "qu-merchant-id": str(merchant_id or "1"),
        "qu-hardware-platform": "3",
        "qu-software-platform": "1",
        "qu-deploy-platform": "1",
        "qu-web-host": "qu.sdo.com",
    }


def _charset_from_content_type(value: str) -> str:
    match = re.search(r"charset=([\w.-]+)", value or "", re.I)
    return match.group(1) if match else "utf-8"


def _extract_anchors(
    html_text: str,
    base_url: str,
    *,
    include_pattern: re.Pattern[str],
    limit: int,
) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r"<a\b([^>]*)>([\s\S]*?)</a>", html_text, re.I):
        href_match = re.search(r"\bhref=[\"']([^\"']+)[\"']", match.group(1), re.I)
        if not href_match:
            continue
        url = urljoin(base_url, html.unescape(href_match.group(1)))
        if not include_pattern.search(url) or url in seen:
            continue
        title = _strip_tags(match.group(2))
        if len(title) < 2:
            continue
        seen.add(url)
        items.append((url, title))
        if len(items) >= limit:
            break
    return items


def _extract_title(html_text: str) -> str:
    match = re.search(r"<title[^>]*>([\s\S]*?)</title>", html_text, re.I)
    return _clean_text(match.group(1) if match else "")


def _extract_meta(html_text: str, name: str) -> str:
    escaped = re.escape(name)
    patterns = [
        rf"<meta[^>]+property=[\"']{escaped}[\"'][^>]+content=[\"']([^\"']*)[\"']",
        rf"<meta[^>]+name=[\"']{escaped}[\"'][^>]+content=[\"']([^\"']*)[\"']",
        rf"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]+(?:property|name)=[\"']{escaped}[\"']",
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.I)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def _extract_product_title(html_text: str) -> str:
    candidates: list[str] = []
    for tag in ("h1", "h2"):
        candidates.extend(
            _strip_tags(match.group(1))
            for match in re.finditer(rf"<{tag}\b[^>]*>([\s\S]{{0,800}}?)</{tag}>", html_text, re.I)
        )
    candidates.extend(
        _strip_tags(match.group(1))
        for match in re.finditer(r"productName\s*[:=]\s*[\"']([^\"']+)[\"']", html_text, re.I)
    )
    generic_titles = {
        "商品详情",
        "商品介紹",
        "商品介绍",
        "商品紹介",
        "關於此商品",
        "アイテムについて",
        "NOTICE",
        "{{product.name}}",
        "FINAL FANTASY XIV ONLINE STORE 繁體中文版 水晶商城",
    }
    for candidate in candidates:
        if len(candidate) >= 2 and candidate not in generic_titles and "{{" not in candidate:
            return candidate
    return ""


def _normalize_page_title(title: str) -> str:
    value = _strip_tags(title)
    parts = [part.strip() for part in re.split(r"\s+\|\s+|\s+-\s+", value) if part.strip()]
    return parts[0] if len(parts) > 1 else value


def _find_price(html_text: str) -> str:
    text = _strip_tags(html_text)
    patterns = [
        r"(?:¥|￥|NT\$|₩)\s?[\d,]+(?:\.\d+)?",
        r"[\d,]+(?:\.\d+)?\s?(?:JPY|KRW|TWD|CNY|円|水晶)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(0).strip()
    return ""


def _strip_tags(value: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", str(value or ""), flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean_text(text)


def _clean_text(value: str) -> str:
    text = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    return re.sub(r"\s+-\s*$", "", text).strip()


def _query_value(url: str, key: str) -> str:
    parsed = urlparse(url)
    for part in parsed.query.split("&"):
        name, _, value = part.partition("=")
        if name == key:
            return value
    return ""


def _path_match(value: str, pattern: str) -> str:
    match = re.search(pattern, value)
    return match.group(1) if match else ""


def _lodestone_detail_id(url: str) -> str:
    match = re.search(r"/detail/([^/?#]+)", url)
    return match.group(1) if match else ""


def _hash_text(value: str) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _hash_json(value: Any) -> str:
    return _hash_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _dedupe_items(items: list[WatchItem]) -> list[WatchItem]:
    seen: set[str] = set()
    result: list[WatchItem] = []
    for item in items:
        key = item.stable_key()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
