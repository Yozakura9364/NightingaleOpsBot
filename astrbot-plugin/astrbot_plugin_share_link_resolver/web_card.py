from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
from bs4 import BeautifulSoup

from .card_renderer import CardContent, ThreadPost


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
NGA_COOKIE_PATH = Path(__file__).resolve().parent / ".local" / "nga_cookies.json"
NGA_SMILEY_BASE_URL = "https://img4.nga.178.com/ngabbs/post/smile"
NGA_SMILEY_FILES = {
    "ac": {
        "blink": "ac0.png",
        "goodjob": "ac1.png",
        "上": "ac2.png",
        "中枪": "ac3.png",
        "偷笑": "ac4.png",
        "冷": "ac5.png",
        "凌乱": "ac6.png",
        "反对": "ac7.png",
        "吓": "ac8.png",
        "吻": "ac9.png",
        "呆": "ac10.png",
        "咦": "ac11.png",
        "哦": "ac12.png",
        "哭": "ac13.png",
        "哭1": "ac14.png",
        "哭笑": "ac15.png",
        "哼": "ac16.png",
        "喘": "ac17.png",
        "喷": "ac18.png",
        "嘲笑": "ac19.png",
        "嘲笑1": "ac20.png",
        "囧": "ac21.png",
        "委屈": "ac22.png",
        "心": "ac23.png",
        "忧伤": "ac24.png",
        "怒": "ac25.png",
        "怕": "ac26.png",
        "惊": "ac27.png",
        "愁": "ac28.png",
        "抓狂": "ac29.png",
        "抠鼻": "ac30.png",
        "擦汗": "ac31.png",
        "无语": "ac32.png",
        "晕": "ac33.png",
        "汗": "ac34.png",
        "瞎": "ac35.png",
        "羞": "ac36.png",
        "羡慕": "ac37.png",
        "花痴": "ac38.png",
        "茶": "ac39.png",
        "衰": "ac40.png",
        "计划通": "ac41.png",
        "赞同": "ac42.png",
        "闪光": "ac43.png",
        "黑枪": "ac44.png",
    },
    "a2": {
        "goodjob": "a2_02.png",
        "偷笑": "a2_03.png",
        "怒": "a2_04.png",
        "诶嘿": "a2_05.png",
        "笑": "a2_07.png",
        "那个…": "a2_08.png",
        "哦嗬嗬嗬": "a2_09.png",
        "舔": "a2_10.png",
        "有何贵干": "a2_11.png",
        "病娇": "a2_12.png",
        "lucky": "a2_13.png",
        "鬼脸": "a2_14.png",
        "大哭": "a2_15.png",
        "冷": "a2_16.png",
        "哭": "a2_17.png",
        "妮可妮可妮": "a2_18.png",
        "惊": "a2_19.png",
        "poi": "a2_20.png",
        "恨": "a2_21.png",
        "囧2": "a2_22.png",
        "中枪": "a2_23.png",
        "囧": "a2_24.png",
        "你看看你": "a2_25.png",
        "yes": "a2_26.png",
        "doge": "a2_27.png",
        "自戳双目": "a2_28.png",
        "偷吃": "a2_30.png",
        "冷笑": "a2_31.png",
        "壁咚": "a2_32.png",
        "不活了": "a2_33.png",
        "不明觉厉": "a2_36.png",
        "jojo立": "a2_37.png",
        "jojo立2": "a2_38.png",
        "jojo立3": "a2_39.png",
        "jojo立5": "a2_40.png",
        "jojo立4": "a2_41.png",
        "威吓": "a2_42.png",
        "你已经死了": "a2_45.png",
        "异议": "a2_47.png",
        "认真": "a2_48.png",
        "你这种人…": "a2_49.png",
        "是在下输了": "a2_51.png",
        "抢镜头": "a2_52.png",
        "你为猴这么": "a2_53.png",
        "干杯": "a2_54.png",
        "干杯2": "a2_55.png",
    },
}


async def fetch_url_card(url: str, *, timeout_seconds: int = 12, max_bytes: int = 1_500_000) -> CardContent:
    parsed = urlparse(url)
    source = platform_label(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return CardContent(source="网页", title="无法识别的链接", url=url)

    try:
        html_text = await _fetch_text(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
        content = _parse_html_card(url, html_text, source=source)
        if content.title or content.description or content.cover_url:
            return content
    except Exception:
        pass
    return CardContent(source=source, title=source, url=url, description="内容抓取失败，已生成链接卡片。")


async def fetch_nga_thread_card(url: str, *, timeout_seconds: int = 12, max_bytes: int = 1_500_000) -> CardContent:
    best: CardContent | None = None
    for candidate in _nga_fetch_candidates(url):
        content = await fetch_url_card(candidate, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
        if content.posts:
            return content
        if best is None or len(content.description) > len(best.description):
            best = content
    return best or CardContent(source="NGA", title="NGA", url=url, description="正文抓取失败，可打开链接查看。")


def platform_label(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "xiaohongshu.com" in host:
        return "小红书"
    if "weibo.com" in host or "weibo.cn" in host:
        return "微博"
    if "nga.cn" in host or "178.com" in host or "ngabbs.com" in host:
        return "NGA"
    if "bilibili.com" in host or host == "b23.tv":
        return "Bilibili"
    return host.removeprefix("www.") or "网页"


def _nga_fetch_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    tid = next((value for key, value in params if key.lower() == "tid" and value), "")
    if not tid:
        return [url]

    canonical = urlunparse(
        parsed._replace(
            scheme="https",
            netloc="ngabbs.com",
            path="/read.php",
            query=urlencode([("tid", tid)]),
            fragment="",
        )
    )
    original = urlunparse(parsed._replace(scheme="https", netloc="ngabbs.com", fragment=""))
    result: list[str] = []
    for candidate in (original, canonical):
        if candidate not in result:
            result.append(candidate)
    return result


async def _fetch_text(url: str, *, timeout_seconds: int, max_bytes: int) -> str:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": _referer_for_url(url),
    }
    cookie_header = _nga_cookie_header() if platform_label(url) == "NGA" else ""
    if cookie_header:
        headers["Cookie"] = cookie_header
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as response:
            data = await response.content.read(max_bytes + 1)
            if len(data) > max_bytes:
                data = data[:max_bytes]
            charset = response.charset or _charset_from_html(data) or _fallback_charset(url)
            text = data.decode(charset, errors="ignore")
            guest_cookie = _nga_guest_cookie(text)
            if response.status == 403 and guest_cookie and platform_label(url) == "NGA":
                retry_headers = dict(headers)
                retry_headers["Cookie"] = _append_cookie_header(cookie_header, f"guestJs={guest_cookie}; lastpath=0")
                retry_url = _append_rand(url)
                async with session.get(retry_url, headers=retry_headers, allow_redirects=True) as retry_response:
                    retry_data = await retry_response.content.read(max_bytes + 1)
                    if len(retry_data) > max_bytes:
                        retry_data = retry_data[:max_bytes]
                    retry_charset = retry_response.charset or _charset_from_html(retry_data) or _fallback_charset(url)
                    return retry_data.decode(retry_charset, errors="ignore")
            return text


async def check_nga_cookie_status(*, timeout_seconds: int = 10) -> dict[str, object]:
    cookie_count = _nga_cookie_count()
    result: dict[str, object] = {
        "exists": NGA_COOKIE_PATH.exists(),
        "cookie_count": cookie_count,
        "logged_in": False,
        "uid": "",
        "mode": "游客",
        "ok": False,
        "error": "",
    }
    if not NGA_COOKIE_PATH.exists():
        return result
    try:
        text = await _fetch_text("https://ngabbs.com/read.php?tid=47056022", timeout_seconds=timeout_seconds, max_bytes=500_000)
        result["ok"] = "访客不能直接访问" not in text
        uid = _current_uid(text)
        if uid:
            result.update({"logged_in": True, "uid": uid, "mode": "已登录"})
        else:
            result["mode"] = "Cookie 存在，但未识别到登录用户"
    except Exception as error:
        result["error"] = type(error).__name__
    return result


def nga_cookie_file_path() -> Path:
    return NGA_COOKIE_PATH


def _referer_for_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return "https://www.google.com/"


def _append_cookie_header(existing: str, extra: str) -> str:
    if existing and extra:
        return f"{existing}; {extra}"
    return existing or extra


def _nga_cookie_count() -> int:
    cookies = _load_nga_cookies()
    if isinstance(cookies, str):
        return len([part for part in cookies.split(";") if part.strip()])
    return len(cookies)


def _nga_cookie_header() -> str:
    cookies = _load_nga_cookies()
    if isinstance(cookies, str):
        return cookies.strip()
    parts = []
    for key, value in cookies.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if key_text and value_text:
            parts.append(f"{key_text}={value_text}")
    return "; ".join(parts)


def _load_nga_cookies() -> dict[str, str] | str:
    if not NGA_COOKIE_PATH.exists():
        return {}
    try:
        raw = NGA_COOKIE_PATH.read_text(encoding="utf-8-sig").strip()
    except Exception:
        return {}
    if not raw:
        return {}
    if not raw.startswith(("{", "[")):
        return raw
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    if isinstance(payload, dict):
        return {str(key): str(value) for key, value in payload.items() if isinstance(value, (str, int, float))}
    if isinstance(payload, list):
        result: dict[str, str] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            domain = str(item.get("domain") or "").lower()
            if name and value and (not domain or "nga" in domain or "178.com" in domain):
                result[name] = value
        return result
    return {}


def _current_uid(text: str) -> str:
    match = re.search(r"__CURRENT_UID\s*=\s*parseInt\('(\d*)'", text)
    return match.group(1) if match and match.group(1) else ""


def _nga_guest_cookie(text: str) -> str:
    match = re.search(r"guestJs=([^;']+)", text)
    return match.group(1) if match else ""


def _append_rand(url: str) -> str:
    parsed = urlparse(url)
    separator = "&" if parsed.query else ""
    query = f"{parsed.query}{separator}rand=123"
    return urlunparse(parsed._replace(query=query))


def _charset_from_html(data: bytes) -> str:
    head = data[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset=['\"]?([a-zA-Z0-9_\-]+)", head, re.IGNORECASE)
    return match.group(1) if match else ""


def _fallback_charset(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "nga.cn" in host or "178.com" in host or "ngabbs.com" in host:
        return "gb18030"
    return "utf-8"


def _parse_html_card(url: str, html_text: str, *, source: str) -> CardContent:
    soup = BeautifulSoup(html_text, "html.parser")
    title = _first_meta(soup, ["og:title", "twitter:title"]) or _tag_text(soup.find("title"))
    description = _first_meta(soup, ["og:description", "description", "twitter:description"])
    cover = _first_meta(soup, ["og:image", "twitter:image", "twitter:image:src"])

    if source == "NGA":
        title = _clean_nga_title(title) or title
        description = _nga_body_excerpt(soup) or description
        posts = _nga_posts(soup, html_text)
        forum = _tag_text(soup.find(id="currentForumName"))
        return CardContent(
            source=source,
            title=_clean_text(title) or (posts[0].title if posts else source),
            url=url,
            description=_clean_text(description),
            cover_url="",
            footer=f"NGA / {forum}" if forum else "NGA",
            posts=tuple(posts),
        )
    elif source == "微博":
        title = _clean_weibo_title(title) or title
        description = _clean_weibo_text(description)

    return CardContent(
        source=source,
        title=_clean_text(title) or source,
        url=url,
        description=_clean_text(description),
        cover_url=cover,
    )


def _first_meta(soup: BeautifulSoup, names: list[str]) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return html.unescape(str(tag["content"]).strip())
    return ""


def _tag_text(tag) -> str:
    return tag.get_text(" ", strip=True) if tag else ""


def _clean_text(text: str, max_chars: int = 500) -> str:
    value = html.unescape(str(text or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_chars]


def _clean_nga_title(title: str) -> str:
    value = _clean_text(title, 180)
    value = re.sub(r"[\-_ ]*NGA玩家社区.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[\-_ ]*艾欧泽亚.*$", "", value)
    return value.strip()


def _nga_body_excerpt(soup: BeautifulSoup) -> str:
    selectors = [
        "#postcontent0",
        ".postcontent",
        ".post_content",
        ".ubbcode",
    ]
    for selector in selectors:
        tag = soup.select_one(selector)
        text = _clean_nga_content(_tag_text(tag))
        if text:
            return text
    return ""


def _nga_posts(soup: BeautifulSoup, html_text: str = "", limit: int = 5) -> list[ThreadPost]:
    result: list[ThreadPost] = []
    source_html = html_text or str(soup)
    users = _nga_user_info_map(source_html)
    uid_name_hints = _nga_uid_name_hint_map(source_html)
    for floor in range(0, 30):
        content_tag = soup.find(id=f"postcontent{floor}")
        if not content_tag:
            continue
        raw_body = _tag_text(content_tag)
        body = _clean_nga_content(raw_body)
        image_urls = _nga_image_urls(raw_body, source_html, floor)
        smiley_urls = _nga_smiley_urls(raw_body)
        if not body and not image_urls and not smiley_urls:
            continue
        title = _clean_text(_tag_text(soup.find(id=f"postsubject{floor}")), 180)
        date = _clean_text(_tag_text(soup.find(id=f"postdate{floor}")), 80)
        uid = _nga_uid(soup, floor)
        user = users.get(uid, {}) if uid else {}
        author = _nga_author(soup, floor, user=user, uid=uid, name_hint=uid_name_hints.get(uid, ""))
        meta = _nga_user_meta(user, date)
        result.append(
            ThreadPost(
                floor=floor,
                author=author or ("楼主" if floor == 0 else "匿名用户"),
                meta=meta or (f"发表于 {date}" if date else ""),
                body=body,
                title=title,
                avatar_url=_nga_avatar_url(_nga_avatar_value(user)),
                image_urls=tuple(image_urls),
                smiley_urls=tuple(smiley_urls),
            )
        )
        if floor == 0:
            hot_posts = _nga_highlight_posts(soup, source_html, users, uid_name_hints, limit=limit - 1)
            if hot_posts:
                result.extend(hot_posts)
                return result[:limit]
        if len(result) >= limit:
            break
    return result


def _nga_highlight_posts(
    soup: BeautifulSoup,
    html_text: str,
    users: dict[str, dict],
    uid_name_hints: dict[str, str],
    *,
    limit: int,
) -> list[ThreadPost]:
    container = soup.find(id="hightlight_for_0")
    if not container or limit <= 0:
        return []

    result: list[ThreadPost] = []
    for comment_tag in container.find_all(id=re.compile(r"^postcomment__")):
        comment_id_match = re.search(r"postcomment__(.+)$", str(comment_tag.get("id") or ""))
        if not comment_id_match:
            continue
        comment_id = comment_id_match.group(1)
        raw_body = comment_tag.get_text("\n", strip=True)
        body = _clean_nga_content(raw_body)
        image_urls: list[str] = []
        smiley_urls = _nga_smiley_urls(raw_body)
        author_tag = container.find(id=f"commentauthor__{comment_id}")
        uid = _uid_from_href(str(author_tag.get("href") or "")) if author_tag else ""
        user = users.get(uid, {}) if uid else {}
        author = _nga_author_from_user(
            user=user,
            uid=uid,
            text=_tag_text(author_tag),
            name_hint=uid_name_hints.get(uid, ""),
        )
        date = _clean_text(_tag_text(container.find(id=f"commentInfo__{comment_id}")), 80)
        hot_score = _nga_comment_hot_score(html_text, comment_id)
        footer_parts: list[str] = []
        if hot_score:
            footer_parts.append(f"赞 {hot_score}")
        if date:
            footer_parts.append(date)
        if not body and not image_urls and not smiley_urls:
            continue
        result.append(
            ThreadPost(
                floor=len(result) + 1,
                author=author or (f"UID:{uid}" if uid else "匿名用户"),
                meta=_nga_user_meta(user, ""),
                body=body,
                likes="  ".join(footer_parts),
                avatar_url=_nga_avatar_url(_nga_avatar_value(user)),
                image_urls=tuple(image_urls),
                smiley_urls=tuple(smiley_urls),
                kind="hot",
            )
        )
        if len(result) >= limit:
            break
    return result


def _nga_uid(soup: BeautifulSoup, floor: int) -> str:
    tag = soup.find(id=f"postauthor{floor}")
    href = str(tag.get("href") or "") if tag else ""
    return _uid_from_href(href)


def _uid_from_href(href: str) -> str:
    match = re.search(r"uid=(\d+)", href)
    return match.group(1) if match else ""


def _nga_author(soup: BeautifulSoup, floor: int, *, user: dict, uid: str, name_hint: str = "") -> str:
    tag = soup.find(id=f"postauthor{floor}")
    text = _clean_text(_tag_text(tag), 80)
    value = _nga_author_from_user(user=user, uid=uid, text=text, name_hint=name_hint)
    if value:
        if floor == 0 and value.startswith("UID:"):
            return f"楼主 {value}"
        return value
    if uid:
        return f"楼主 UID:{uid}" if floor == 0 else f"UID:{uid}"
    return ""


def _nga_author_from_user(*, user: dict, uid: str, text: str = "", name_hint: str = "") -> str:
    username = _clean_text(str(user.get("username") or ""), 80)
    nickname = _clean_text(str(user.get("nickname") or ""), 80)
    hinted_name = _clean_text(name_hint, 80)
    value = username or text or hinted_name
    if _is_anonymous_uid(value) and hinted_name and not _is_anonymous_uid(hinted_name):
        value = hinted_name
    if _is_anonymous_uid(value) and nickname and not _is_anonymous_uid(nickname):
        value = nickname
    if value:
        return value
    if uid:
        return f"UID:{uid}"
    return ""


def _nga_comment_hot_score(html_text: str, comment_id: str) -> str:
    marker = re.escape(comment_id)
    pattern = rf"commonui\.postArg\.proc\(\s*'__{marker}'.*?'0,(\d+),0'"
    match = re.search(pattern, html_text, flags=re.DOTALL)
    if not match:
        return ""
    score = int(match.group(1))
    return str(score) if score > 0 else ""


def _nga_user_meta(user: dict, date: str) -> str:
    parts: list[str] = []
    rvrc = user.get("rvrc")
    if isinstance(rvrc, (int, float)):
        parts.append(f"威望 {rvrc / 10:g}")
    postnum = user.get("postnum")
    if isinstance(postnum, int):
        parts.append(f"{postnum}帖")
    regdate = user.get("regdate")
    if isinstance(regdate, int) and regdate > 0:
        parts.append(f"注册 {_date_from_timestamp(regdate)}")
    if date:
        parts.append(f"发表于 {date}")
    return " | ".join(parts)


def _is_anonymous_uid(value: str) -> bool:
    return bool(re.fullmatch(r"UID:?\d+", str(value or "").strip(), flags=re.IGNORECASE))


def _nga_uid_name_hint_map(html_text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r"\[uid=(-?\d+)\]([^\[]+?)\[/uid\]", html_text):
        uid = match.group(1)
        name = _clean_text(match.group(2), 80)
        if uid and name and not _is_anonymous_uid(name):
            result.setdefault(uid, name)
    return result


def _nga_image_urls(raw_body: str, html_text: str, floor: int) -> list[str]:
    result: list[str] = []
    for match in re.finditer(r"\[img\](.*?)\[/img\]", raw_body, flags=re.IGNORECASE | re.DOTALL):
        url = _nga_attachment_url(match.group(1))
        if url and url not in result:
            result.append(url)

    pattern = rf"ubbcode\.attach\.load\('postattach{floor}','postcontent{floor}',\[(.*?)\]"
    attach_match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
    if attach_match:
        for url_match in re.finditer(r"url:'([^']+)'", attach_match.group(1)):
            url = _nga_attachment_url(url_match.group(1))
            if url and url not in result:
                result.append(url)
    return result


def _nga_attachment_url(value: str) -> str:
    url = html.unescape(str(value or "")).strip().strip("'\"")
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    url = url.removeprefix("./").lstrip("/")
    if url.startswith("attachments/"):
        url = url[len("attachments/") :]
    return f"https://img.nga.178.com/attachments/{url}"


def _nga_smiley_urls(text: str) -> list[str]:
    result: list[str] = []
    for match in re.finditer(r"\[s:([^\]]+)\]", str(text or ""), flags=re.IGNORECASE):
        url = _nga_smiley_url(match.group(1))
        if url and url not in result:
            result.append(url)
    return result


def _nga_smiley_url(value: str) -> str:
    parts = [part.strip() for part in str(value or "").split(":") if part.strip()]
    if not parts:
        return ""
    if len(parts) == 1 and parts[0].isdigit():
        return f"{NGA_SMILEY_BASE_URL}/{int(parts[0]):02d}.gif"
    group = parts[0].lower()
    name = parts[-1]
    filename = NGA_SMILEY_FILES.get(group, {}).get(name)
    if not filename:
        return ""
    return f"{NGA_SMILEY_BASE_URL}/{filename}"


def _nga_smiley_label(value: str) -> str:
    if _nga_smiley_url(value):
        return ""
    parts = [part for part in str(value or "").split(":") if part]
    return f"〔{parts[-1] if parts else '表情'}〕"


def _date_from_timestamp(value: int) -> str:
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _nga_avatar_value(user: dict):
    for key in ("avatar", "avatarurl", "avatar_url", "face", "icon"):
        value = user.get(key)
        if value:
            return value
    return ""


def _nga_avatar_url(value) -> str:
    avatar = str(value or "").strip()
    if not avatar:
        return ""
    if avatar.startswith("//"):
        return "https:" + avatar
    if avatar.startswith("http://") or avatar.startswith("https://"):
        return avatar
    if avatar.startswith("./"):
        return "https://ngabbs.com/" + avatar[2:]
    if avatar.startswith("/"):
        return "https://ngabbs.com" + avatar
    return "https://ngabbs.com/" + avatar.lstrip("/")


def _nga_user_info_map(html_text: str) -> dict[str, dict]:
    marker = "commonui.userInfo.setAll("
    start_call = html_text.find(marker)
    if start_call < 0:
        return {}
    start = html_text.find("{", start_call)
    if start < 0:
        return {}
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(html_text)):
        char = html_text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(html_text[start : index + 1])
                    return {str(key): value for key, value in data.items() if isinstance(value, dict)}
                except Exception:
                    return {}
    return {}


def _clean_nga_content(text: str) -> str:
    value = html.unescape(str(text or ""))
    replacements = [
        (r"\[br\]", "\n"),
        (r"\[/quote\]", "\n"),
        (r"\[quote\]", ""),
        (r"\[img\].*?\[/img\]", ""),
        (r"\[s:([^\]]+)\]", lambda match: _nga_smiley_label(match.group(1))),
        (r"\[url(?:=[^\]]+)?\](.*?)\[/url\]", r"\1"),
        (r"\[(?:/?)(?:h|align|size|b|i|u|color|collapse|del|font|list|\\*)[^\]]*\]", ""),
    ]
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"\[[a-zA-Z0-9_/*=\-:#%,.\s]+\]", "", value)
    value = re.sub(r"\s*\n\s*", "\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = value.strip()
    if len(value) > 620:
        value = value[:617].rstrip() + "..."
    return value


def _clean_weibo_title(title: str) -> str:
    value = _clean_text(title, 180)
    value = re.sub(r"[\-_ ]*微博.*$", "", value)
    return value.strip()


def _clean_weibo_text(text: str) -> str:
    value = _clean_text(text, 520)
    value = re.sub(r"网页链接$", "", value).strip()
    return value
