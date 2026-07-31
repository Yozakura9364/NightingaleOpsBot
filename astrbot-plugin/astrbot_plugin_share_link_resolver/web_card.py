from __future__ import annotations

import html
import hashlib
import json
import random
import re
import time
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
NGA_SMILEY_TOKEN_PREFIX = "\x1eNGA_SMILEY:"
NGA_SMILEY_TOKEN_SUFFIX = "\x1f"
XIAOHEIHE_DICT = "JKMNPQRTX1234OABCDFG56789H"
XIAOHEIHE_WEB_DICT = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
TAPTAP_X_UA = "V=1&PN=WebApp&VN=0.1.0&LANG=zh_CN&PLT=PC"
XIAOHEIHE_EMOJI_MAP = {
    "委屈": "😭",
    "哭": "😭",
    "大哭": "😭",
    "笑": "😄",
    "开心": "😄",
    "生气": "😡",
    "疑问": "？",
}
KUROBBS_EMOJI_MAP = {
    "爱你": "💕",
    "喜欢": "😍",
    "祈祷": "🙏",
    "有点意思": "😏",
    "厉害": "👍",
    "不了": "",
    "拖走": "",
    "收了你": "",
}
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
    html_text = ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return CardContent(source="网页", title="无法识别的链接", url=url)

    try:
        if source == "Bilibili":
            content = await fetch_bilibili_video_card(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
            if content.cover_url or content.title or content.description:
                return content
        if source == "米游社":
            content = await fetch_miyoushe_article_card(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
            if content.posts:
                return content
        if source == "TapTap":
            content = await fetch_taptap_topic_card(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
            if content.posts:
                return content
        if source == "库街区":
            content = await fetch_kurobbs_article_card(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
            if content.posts:
                return content
        if source == "小黑盒":
            content = await fetch_xiaoheihe_article_card(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
            if content.posts:
                return content
    except Exception:
        pass

    try:
        if not html_text:
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


async def fetch_miyoushe_article_card(url: str, *, timeout_seconds: int = 12, max_bytes: int = 1_500_000) -> CardContent:
    post_id = _miyoushe_post_id(url)
    if not post_id:
        return CardContent(source="米游社", title="米游社", url=url)
    api_url = "https://bbs-api.miyoushe.com/post/wapi/getPostFull?" + urlencode({"post_id": post_id, "read": 1})
    payload = await _fetch_json(api_url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, referer=url)
    data = payload.get("data") if isinstance(payload, dict) else {}
    wrapper = data.get("post") if isinstance(data, dict) and isinstance(data.get("post"), dict) else {}
    post = wrapper.get("post") if isinstance(wrapper.get("post"), dict) else wrapper
    user = wrapper.get("user") if isinstance(wrapper.get("user"), dict) else data.get("user", {}) if isinstance(data, dict) else {}
    stat = wrapper.get("stat") if isinstance(wrapper.get("stat"), dict) else data.get("stat", {}) if isinstance(data, dict) else {}
    forum = wrapper.get("forum") if isinstance(wrapper.get("forum"), dict) else data.get("forum", {}) if isinstance(data, dict) else {}

    title = _clean_text(_first_value(post, ("subject", "title")), 180)
    body, content_images = _content_text_and_images(post.get("content", ""))
    images = _unique_urls(
        content_images
        + _images_from_value(data.get("image_list") if isinstance(data, dict) else None)
        + _images_from_value(post.get("images"))
        + _images_from_value(post.get("cover"))
        + _images_from_value(data.get("cover") if isinstance(data, dict) else None)
    )
    author = _clean_text(_first_value(user, ("nickname", "name", "username")), 80)
    avatar = _first_image_value(user, ("avatar_url", "avatar", "icon"))
    meta = _join_nonempty(
        [
            _clean_text(_first_value(forum, ("name", "forum_name")), 80),
            _date_from_timestamp(_safe_int(post.get("created_at"))),
            _stat_text(stat, (("评论", "reply_num", "reply_count", "comment_num"), ("赞", "like_num", "like_count"), ("收藏", "bookmark_num", "fav_num"))),
        ],
        "  ",
    )
    main_post = ThreadPost(
        floor=0,
        author=author or "米游社用户",
        meta=meta,
        body=_clean_article_body(body),
        title=title,
        likes=_stat_text(stat, (("评论", "reply_num", "reply_count", "comment_num"), ("赞", "like_num", "like_count"))),
        avatar_url=avatar,
        image_urls=tuple(images[:5]),
        kind="article",
    )
    if not main_post.body and not main_post.image_urls:
        return CardContent(source="米游社", title=title or "米游社", url=url)
    return CardContent(
        source="米游社",
        title=title or "米游社",
        url=url,
        description=main_post.body,
        cover_url=images[0] if images else "",
        author=author,
        footer="米游社主楼",
        posts=(main_post,),
    )


async def fetch_bilibili_video_card(url: str, *, timeout_seconds: int = 12, max_bytes: int = 1_500_000) -> CardContent:
    bvid = _bilibili_bvid(url)
    html_text = ""
    if not bvid:
        html_text = await _fetch_text(url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
        bvid = _bilibili_bvid_from_html(html_text)
    if not bvid:
        if html_text:
            return _parse_bilibili_html_card(url, html_text, title="", description="", cover="")
        return CardContent(source="Bilibili", title="Bilibili", url=url)
    api_url = "https://api.bilibili.com/x/web-interface/view?" + urlencode({"bvid": bvid})
    payload = await _fetch_json(api_url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, referer=url)
    data = payload.get("data") if isinstance(payload, dict) else {}
    owner = _first_dict(data, ("owner",))
    cover_url = _first_image_value(data, ("pic", "cover"))
    if _is_bilibili_placeholder_image(cover_url):
        cover_url = ""
    return CardContent(
        source="Bilibili",
        title=_clean_text(_first_value(data, ("title",)), 180) or "Bilibili",
        url=url,
        description=_clean_text(_first_value(data, ("desc", "description")), 600),
        cover_url=cover_url,
        author=_clean_text(_first_value(owner, ("name",)), 80),
        footer="Bilibili",
    )


async def fetch_taptap_topic_card(url: str, *, timeout_seconds: int = 12, max_bytes: int = 1_500_000) -> CardContent:
    topic_id = _taptap_topic_id(url)
    if not topic_id:
        return CardContent(source="TapTap", title="TapTap", url=url)
    api_url = "https://www.taptap.cn/webapiv2/topic/v1/detail?" + urlencode({"id": topic_id, "X-UA": TAPTAP_X_UA})
    payload = await _fetch_json(api_url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, referer=_taptap_referer(url))
    data = payload.get("data") if isinstance(payload, dict) else {}
    topic = _first_dict(data, ("topic", "post", "data")) or data if isinstance(data, dict) else {}
    first_post = _first_dict(data, ("first_post", "firstPost", "content", "post")) or topic
    user = _first_dict(topic, ("user", "author")) or _first_dict(first_post, ("user", "author")) or {}
    title = _clean_text(_first_value(topic, ("title", "subject")) or _first_value(first_post, ("title", "subject")), 180)
    body, images = _content_text_and_images(_first_existing(first_post, ("contents", "content", "summary", "text", "body")))
    images = _unique_urls(images + _images_from_value(topic) + _images_from_value(first_post))[:5]
    author = _clean_text(_first_value(user, ("name", "username", "nickname")), 80)
    meta = _join_nonempty(
        [
            _date_from_timestamp(_safe_int(_first_value(topic, ("created_at", "createdAt", "created_time", "createdTime")) or _first_value(first_post, ("created_at", "createdAt", "created_time", "createdTime")))),
            _stat_text(topic, (("回复", "comments_count", "comment_count", "reply_count"), ("赞", "likes_count", "like_count"), ("收藏", "collections_count", "collect_count"))),
        ],
        "  ",
    )
    main_post = ThreadPost(
        floor=0,
        author=author or "TapTap 用户",
        meta=meta,
        body=_clean_article_body(body),
        title=title,
        avatar_url=_first_image_value(user, ("avatar", "avatar_url", "photo")),
        image_urls=tuple(images),
        kind="article",
    )
    if not main_post.body and not main_post.image_urls:
        return CardContent(source="TapTap", title=title or "TapTap", url=url)
    return CardContent(
        source="TapTap",
        title=title or "TapTap",
        url=url,
        description=main_post.body,
        cover_url=images[0] if images else "",
        author=author,
        footer="TapTap 主楼",
        posts=(main_post,),
    )


async def fetch_xiaoheihe_article_card(url: str, *, timeout_seconds: int = 12, max_bytes: int = 1_500_000) -> CardContent:
    link_id = _xiaoheihe_link_id(url)
    if not link_id:
        return CardContent(source="小黑盒", title="小黑盒", url=url)
    tree_url = _xiaoheihe_web_signed_url(
        "/bbs/app/link/tree",
        {
            "os_type": "web",
            "app": "heybox",
            "client_type": "web",
            "version": "999.0.4",
            "web_version": "2.5",
            "x_client_type": "web",
            "x_app": "heybox_website",
            "heybox_id": "",
            "x_os_type": "Mac",
            "link_id": link_id,
        },
    )
    payload = await _fetch_json(tree_url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, referer=url)
    result = payload.get("result") if isinstance(payload, dict) else {}
    item = _first_dict(result, ("link", "data", "detail", "post")) or result if isinstance(result, dict) else {}
    card = _xiaoheihe_card_from_item(url, item)
    if card and card.posts:
        return card

    api_url = _xiaoheihe_signed_url(
        "https://api.xiaoheihe.cn/bbs/app/api/share/data/?"
        + urlencode(
            {
                "os_type": "web",
                "app": "heybox",
                "client_type": "mobile",
                "version": "999.0.3",
                "x_client_type": "web",
                "x_os_type": "Mac",
                "x_app": "heybox",
                "heybox_id": "-1",
                "offset": "0",
                "limit": "3",
                "link_id": link_id,
                "use_concept_type": "",
            }
        )
    )
    payload = await _fetch_json(api_url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, referer=url)
    result = payload.get("result") if isinstance(payload, dict) else {}
    item = _first_dict(result, ("link", "data", "detail", "post")) or result if isinstance(result, dict) else {}
    if str(payload.get("status") or "").lower() == "failed" and not item:
        return CardContent(source="小黑盒", title="小黑盒", url=url)
    card = _xiaoheihe_card_from_item(url, item)
    if card:
        return card
    return CardContent(source="小黑盒", title="小黑盒", url=url)


async def fetch_kurobbs_article_card(url: str, *, timeout_seconds: int = 12, max_bytes: int = 1_500_000) -> CardContent:
    post_id = _kurobbs_post_id(url)
    if not post_id:
        return CardContent(source="库街区", title="库街区", url=url)

    api_url = "https://api.kurobbs.com/forum/getPostDetail"
    form = {
        "isOnlyPublisher": "0",
        "postId": post_id,
        "showOrderType": "2",
    }
    headers = {
        "User-Agent": "okhttp/3.10.0",
        "Accept": "application/json,text/plain,*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "osversion": "Android",
        "devcode": "2fba3859fe9bfe9099f2696b8648c2c6",
        "distinct_id": "765485e7-30ce-4496-9a9c-a2ac1c03c02c",
        "countrycode": "CN",
        "model": "2211133C",
        "source": "android",
        "lang": "zh-Hans",
        "version": "1.0.9",
        "versioncode": "1090",
        "token": "",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(api_url, data=form) as response:
            data = await _read_limited(response, max_bytes)
    payload = json.loads(data.decode("utf-8", errors="ignore"))
    detail = payload.get("data", {}).get("postDetail", {}) if isinstance(payload, dict) else {}
    if not isinstance(detail, dict) or not detail:
        return CardContent(source="库街区", title="库街区", url=url)

    title = _clean_text(_clean_kurobbs_inline_text(_first_value(detail, ("postTitle", "title"))), 180)
    body, images = _kurobbs_content_text_and_images(detail.get("postContent"))
    if not body:
        body, images = _content_text_and_images(detail.get("postH5Content") or detail.get("postNewH5Content"))
        body = _clean_kurobbs_inline_text(body)
    images = _unique_urls(images + _images_from_value(detail.get("postH5Content")) + _images_from_value(detail.get("postNewH5Content")))[:5]
    author = _clean_text(_first_value(detail, ("userName", "nickname", "name")), 80)
    forum = _first_dict(detail, ("gameForumVo",))
    meta = _join_nonempty(
        [
            _clean_text(_first_value(detail, ("gameName",)), 40),
            _clean_text(_first_value(forum, ("name",)), 40),
            _clean_text(_first_value(detail, ("postTime",)), 40),
            _stat_text(detail, (("评论", "commentCount"), ("赞", "likeCount"), ("收藏", "collectionCount"))),
        ],
        "  ",
    )
    main_post = ThreadPost(
        floor=0,
        author=author or "库街区用户",
        meta=meta,
        body=_clean_article_body(body),
        title=title,
        avatar_url=_first_image_value(detail, ("headCodeUrl", "userHeadUrl", "headFrameUrl")),
        image_urls=tuple(images),
        kind="article",
    )
    if not main_post.body and not main_post.image_urls:
        return CardContent(source="库街区", title=title or "库街区", url=url)
    return CardContent(
        source="库街区",
        title=title or "库街区",
        url=url,
        description=main_post.body,
        cover_url=images[0] if images else "",
        author=author,
        footer="库街区",
        posts=(main_post,),
    )


def _xiaoheihe_card_from_item(url: str, item: dict) -> CardContent | None:
    if not isinstance(item, dict) or not item:
        return None
    title = _clean_text(_clean_xiaoheihe_inline_text(_first_value(item, ("title", "subject"))), 180)
    body, content_images = _content_text_and_images(_first_existing(item, ("content", "text", "description", "desc", "hb_rich_texts")))
    images = _unique_urls(content_images + _images_from_value(item.get("imgs")) + _images_from_value(item.get("thumbs")) + _images_from_value(item.get("cover")))
    user = _first_dict(item, ("user", "up", "author")) or {}
    author = _clean_text(_first_value(user, ("nickname", "name", "username")), 80)
    meta = _join_nonempty(
        [
            _clean_text(_first_value(item, ("formated_time", "formatted_time")), 80),
            _stat_text(item, (("评论", "comment_num", "comment_count"), ("赞", "link_award_num", "like_count"), ("转发", "forward_num"))),
        ],
        "  ",
    )
    main_post = ThreadPost(
        floor=0,
        author=author or "小黑盒用户",
        meta=meta,
        body=_clean_article_body(body),
        title=title,
        avatar_url=_first_image_value(user, ("avatar", "avatar_url", "head_img")),
        image_urls=tuple(images[:5]),
        kind="article",
    )
    if not main_post.body and not main_post.image_urls:
        return CardContent(source="小黑盒", title=title or "小黑盒", url=url)
    return CardContent(
        source="小黑盒",
        title=title or "小黑盒",
        url=url,
        description=main_post.body,
        cover_url=images[0] if images else "",
        author=author,
        footer="小黑盒",
        posts=(main_post,),
    )


def platform_label(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "xiaohongshu.com" in host:
        return "小红书"
    if "skland.com" in host:
        return "森空岛"
    if "weibo.com" in host or "weibo.cn" in host:
        return "微博"
    if "miyoushe.com" in host:
        return "米游社"
    if "taptap.cn" in host or "taptap.io" in host:
        return "TapTap"
    if "kurobbs.com" in host:
        return "库街区"
    if "xiaoheihe.cn" in host or "heybox" in host:
        return "小黑盒"
    if "nga.cn" in host or "178.com" in host or "ngabbs.com" in host:
        return "NGA"
    if "bilibili.com" in host or host == "b23.tv":
        return "Bilibili"
    return host.removeprefix("www.") or "网页"


def _bilibili_bvid(url: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    candidate = str(query.get("bvid") or query.get("BVID") or "").strip()
    if re.fullmatch(r"BV[0-9A-Za-z]+", candidate):
        return candidate
    path_match = re.search(r"/video/(BV[0-9A-Za-z]+)", parsed.path, flags=re.IGNORECASE)
    if path_match:
        return path_match.group(1)
    return ""


def _bilibili_bvid_from_html(html_text: str) -> str:
    match = re.search(r'"bvid"\s*:\s*"(BV[0-9A-Za-z]+)"', html_text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"/video/(BV[0-9A-Za-z]+)", html_text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


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
            data = await _read_limited(response, max_bytes)
            charset = response.charset or _charset_from_html(data) or _fallback_charset(url)
            text = data.decode(charset, errors="ignore")
            guest_cookie = _nga_guest_cookie(text)
            if response.status == 403 and guest_cookie and platform_label(url) == "NGA":
                retry_headers = dict(headers)
                retry_headers["Cookie"] = _append_cookie_header(cookie_header, f"guestJs={guest_cookie}; lastpath=0")
                retry_url = _append_rand(url)
                async with session.get(retry_url, headers=retry_headers, allow_redirects=True) as retry_response:
                    retry_data = await _read_limited(retry_response, max_bytes)
                    retry_charset = retry_response.charset or _charset_from_html(retry_data) or _fallback_charset(url)
                    return retry_data.decode(retry_charset, errors="ignore")
            return text


async def _fetch_json(url: str, *, timeout_seconds: int, max_bytes: int, referer: str = "") -> dict:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": referer or _referer_for_url(url),
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as response:
            data = await _read_limited(response, max_bytes)
            text = data.decode(response.charset or "utf-8", errors="ignore")
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else {}


async def _read_limited(response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(65536):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            break
    data = b"".join(chunks)
    return data[:max_bytes]


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
        parts: list[str] = []
        for part in raw.split(";"):
            segment = part.strip()
            if not segment or "=" not in segment:
                continue
            key, value = segment.split("=", 1)
            key_text = str(key).strip()
            value_text = re.sub(r"\s+", "", str(value))
            if key_text and value_text:
                parts.append(f"{key_text}={value_text}")
        return "; ".join(parts)
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    if isinstance(payload, dict):
        return {
            str(key): re.sub(r"\s+", "", str(value))
            for key, value in payload.items()
            if isinstance(value, (str, int, float)) and re.sub(r"\s+", "", str(value))
        }
    if isinstance(payload, list):
        result: dict[str, str] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = re.sub(r"\s+", "", str(item.get("value") or ""))
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


def _miyoushe_post_id(url: str) -> str:
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 3 and path_parts[1] == "article" and path_parts[2].isdigit():
        return path_parts[2]
    fragment_parts = [part for part in parsed.fragment.split("/") if part]
    if len(fragment_parts) >= 2 and fragment_parts[0] == "article" and fragment_parts[1].isdigit():
        return fragment_parts[1]
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return params.get("post_id", "") if params.get("post_id", "").isdigit() else ""


def _taptap_topic_id(url: str) -> str:
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("id", "topic_id", "topicId"):
        value = str(params.get(key) or "")
        if value.isdigit():
            return value
    for part in reversed([part for part in parsed.path.split("/") if part]):
        if part.isdigit():
            return part
    return ""


def _taptap_referer(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts):
        if part == "app" and index + 1 < len(parts) and parts[index + 1].isdigit():
            return f"https://www.taptap.cn/app/{parts[index + 1]}"
    return "https://www.taptap.cn/"


def _kurobbs_post_id(url: str) -> str:
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("postId", "post_id", "id"):
        value = str(params.get(key) or "").strip()
        if value.isdigit():
            return value
    for part in reversed([part for part in parsed.path.split("/") if part]):
        if part.isdigit():
            return part
    return ""


def _xiaoheihe_link_id(url: str) -> str:
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("link_id", "linkid", "linkId"):
        value = str(params.get(key) or "").strip()
        if re.fullmatch(r"[a-zA-Z0-9]+", value):
            return value
    for part in reversed([part for part in parsed.path.split("/") if part]):
        if re.fullmatch(r"[a-zA-Z0-9]{6,}", part):
            return part
    return ""


def _first_dict(value, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        return {}
    for key in keys:
        item = value.get(key)
        if isinstance(item, dict):
            return item
    return {}


def _first_existing(value, keys: tuple[str, ...]):
    if not isinstance(value, dict):
        return ""
    for key in keys:
        if key in value and value.get(key) not in (None, ""):
            return value.get(key)
    return ""


def _first_value(value, keys: tuple[str, ...]) -> str:
    if not isinstance(value, dict):
        return ""
    for key in keys:
        item = value.get(key)
        if isinstance(item, (str, int, float)) and str(item).strip():
            return str(item)
    return ""


def _first_image_value(value, keys: tuple[str, ...]) -> str:
    if not isinstance(value, dict):
        return ""
    for key in keys:
        urls = _images_from_value(value.get(key))
        if urls:
            return urls[0]
    return ""


def _content_text_and_images(value) -> tuple[str, list[str]]:
    if value is None:
        return "", []
    if isinstance(value, str):
        text = html.unescape(value).strip()
        if not text:
            return "", []
        if text.startswith(("{", "[")):
            try:
                return _content_text_and_images(json.loads(text))
            except Exception:
                pass
        if "<" in text and ">" in text:
            soup = BeautifulSoup(text, "html.parser")
            return _clean_text(soup.get_text("\n", strip=True), 4000), _images_from_value(text)
        return text, _images_from_value(text)
    if isinstance(value, list):
        texts: list[str] = []
        images: list[str] = []
        for item in value:
            text, item_images = _content_text_and_images(item)
            if text:
                texts.append(text)
            images.extend(item_images)
        return "\n".join(texts), _unique_urls(images)
    if isinstance(value, dict):
        images = _images_from_value(value)
        text_values: list[str] = []
        for key in ("describe", "text", "content", "title", "summary", "value", "raw_text", "desc"):
            item = value.get(key)
            if isinstance(item, str) and item.strip() and not _looks_like_image_url(item):
                text, item_images = _content_text_and_images(item)
                if text:
                    text_values.append(text)
                images.extend(item_images)
            elif isinstance(item, (dict, list)):
                text, item_images = _content_text_and_images(item)
                if text:
                    text_values.append(text)
                images.extend(item_images)
        for key in ("contents", "nodes", "paragraphs", "blocks", "children"):
            text, item_images = _content_text_and_images(value.get(key))
            if text:
                text_values.append(text)
            images.extend(item_images)
        return "\n".join(text_values), _unique_urls(images)
    return str(value), []


def _kurobbs_content_text_and_images(value) -> tuple[str, list[str]]:
    if not isinstance(value, list):
        return "", []
    texts: list[str] = []
    images: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if _safe_int(item.get("contentType")) == 2 and item.get("url"):
            images.extend(_images_from_value(item.get("url")))
            continue
        text = _clean_kurobbs_inline_text(str(item.get("content") or ""))
        if text:
            texts.append(text)
        images.extend(_images_from_value(item))
    return "\n".join(texts), _unique_urls(images)


def _images_from_value(value) -> list[str]:
    result: list[str] = []
    if value is None:
        return result
    if isinstance(value, str):
        text = html.unescape(value)
        if _looks_like_image_url(text):
            result.append(_normalize_image_url(text))
        result.extend(_normalize_image_url(match.group(0)) for match in re.finditer(r"https?://[^\s\"'<>]+?\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s\"'<>]*)?", text, flags=re.IGNORECASE))
        return _unique_urls(result)
    if isinstance(value, list):
        for item in value:
            result.extend(_images_from_value(item))
        return _unique_urls(result)
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in ("image", "img", "cover", "thumb", "url", "src", "avatar", "photo")):
                result.extend(_images_from_value(item))
            elif isinstance(item, (dict, list)):
                result.extend(_images_from_value(item))
        return _unique_urls(result)
    return result


def _looks_like_image_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:$|\?)", parsed.path + ("?" + parsed.query if parsed.query else ""), flags=re.IGNORECASE))


def _normalize_image_url(value: str) -> str:
    url = html.unescape(str(value or "")).strip().strip("'\"")
    if url.startswith("//"):
        return "https:" + url
    return url


def _unique_urls(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = _normalize_image_url(value)
        if not url or url.lower() in seen:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        seen.add(url.lower())
        result.append(url)
    return result


def _clean_article_body(text: str, max_chars: int = 1200) -> str:
    value = _clean_xiaoheihe_inline_text(html.unescape(str(text or "")))
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"(?im)^\s*/(?:storage|sdcard|data|cache|Android)/\S+\.(?:jpg|jpeg|png|webp|gif)(?:\?\S*)?\s*$", "", value)
    value = re.sub(r"(?<!\S)/(?:storage|sdcard|data|cache|Android)/\S+\.(?:jpg|jpeg|png|webp|gif)(?:\?\S*)?", "", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if len(value) > max_chars:
        value = value[: max_chars - 3].rstrip() + "..."
    return value


def _clean_xiaoheihe_inline_text(text: str) -> str:
    def replace_cube(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        return XIAOHEIHE_EMOJI_MAP.get(name, "")

    return re.sub(r"\[cube_([^\]]+)\]", replace_cube, str(text or ""))


def _clean_kurobbs_inline_text(text: str) -> str:
    def replace_emote(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if "-" in name:
            name = name.rsplit("-", 1)[-1]
        return KUROBBS_EMOJI_MAP.get(name, "")

    value = re.sub(r"_\[/([^\]]+)\]", replace_emote, str(text or ""))
    value = value.replace("|||", "\n")
    return value.strip()


def _join_nonempty(values: list[str], separator: str) -> str:
    return separator.join(str(value).strip() for value in values if str(value or "").strip())


def _safe_int(value) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _stat_text(source: dict, specs: tuple[tuple[str, ...], ...]) -> str:
    if not isinstance(source, dict):
        return ""
    parts: list[str] = []
    for spec in specs:
        label, *keys = spec
        for key in keys:
            value = _safe_int(source.get(key))
            if value > 0:
                parts.append(f"{label} {value}")
                break
    return "  ".join(parts)


def _xiaoheihe_signed_url(url: str) -> str:
    timestamp = int(time.time())
    nonce = hashlib.md5(str(random.random()).encode("utf-8")).hexdigest().upper()
    parsed = urlparse(url)
    path = "/" + "/".join(part for part in parsed.path.split("/") if part) + "/"
    nonce_seed = "".join(char for char in nonce + XIAOHEIHE_DICT if char.isdigit())
    nonce_hash = hashlib.md5(nonce_seed.encode("utf-8")).hexdigest().lower()
    random_digits = hashlib.md5(f"{timestamp + 1}{path}{nonce_hash}".encode("utf-8")).hexdigest()
    random_digits = "".join(char for char in random_digits if char.isdigit())[:9].ljust(9, "0")
    number = int(random_digits)
    key = ""
    for _ in range(5):
        index = number % len(XIAOHEIHE_DICT)
        number //= len(XIAOHEIHE_DICT)
        key += XIAOHEIHE_DICT[index]
    hkey = key + str(_xiaoheihe_checksum([ord(char) for char in key[-4:]])).zfill(2)
    query = parsed.query + ("&" if parsed.query else "") + urlencode({"hkey": hkey, "_time": timestamp, "nonce": nonce})
    return urlunparse(parsed._replace(query=query))


def _xiaoheihe_web_signed_url(path: str, params: dict[str, str]) -> str:
    timestamp = int(time.time())
    nonce = hashlib.md5(f"{timestamp}{random.random()}".encode("utf-8")).hexdigest().upper()
    signed_params = dict(params)
    signed_params.update(
        {
            "hkey": _xiaoheihe_web_hkey(path, timestamp + 1, nonce),
            "_time": str(timestamp),
            "nonce": nonce,
        }
    )
    normalized_path = "/" + "/".join(part for part in path.split("/") if part)
    return "https://api.xiaoheihe.cn" + normalized_path + "?" + urlencode(signed_params)


def _xiaoheihe_web_hkey(path: str, timestamp: int, nonce: str) -> str:
    normalized_path = "/" + "/".join(part for part in path.split("/") if part) + "/"
    seed = _xiaoheihe_interleave(
        [
            _xiaoheihe_web_encode(str(timestamp), -2),
            _xiaoheihe_web_scramble(normalized_path),
            _xiaoheihe_web_scramble(nonce),
        ]
    )[:20]
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    checksum = _xiaoheihe_checksum([ord(char) for char in digest[-6:]])
    return _xiaoheihe_web_encode(digest[:5], -4) + str(checksum).zfill(2)


def _xiaoheihe_web_encode(value: str, slice_end: int) -> str:
    chars = XIAOHEIHE_WEB_DICT[:slice_end]
    return "".join(chars[ord(char) % len(chars)] for char in value)


def _xiaoheihe_web_scramble(value: str) -> str:
    return "".join(XIAOHEIHE_WEB_DICT[ord(char) % len(XIAOHEIHE_WEB_DICT)] for char in value)


def _xiaoheihe_interleave(values: list[str]) -> str:
    result: list[str] = []
    for index in range(max(len(value) for value in values)):
        for value in values:
            if index < len(value):
                result.append(value[index])
    return "".join(result)


def _xiaoheihe_checksum(data: list[int]) -> int:
    transformed = [
        _xhh_c0(data[0]) ^ _xhh_c1(data[1]) ^ _xhh_c2(data[2]) ^ _xhh_c3(data[3]),
        _xhh_c3(data[0]) ^ _xhh_c0(data[1]) ^ _xhh_c1(data[2]) ^ _xhh_c2(data[3]),
        _xhh_c2(data[0]) ^ _xhh_c3(data[1]) ^ _xhh_c0(data[2]) ^ _xhh_c1(data[3]),
        _xhh_c1(data[0]) ^ _xhh_c2(data[1]) ^ _xhh_c3(data[2]) ^ _xhh_c0(data[3]),
    ]
    return (sum(transformed) + sum(data[4:])) % 100




def _xhh_convert(value: int) -> int:
    return (0xFF & ((value << 1) ^ 0x1B)) if value & 0x80 else value << 1


def _xhh_c3(value: int) -> int:
    return _xhh_convert(value) ^ value


def _xhh_c2(value: int) -> int:
    return _xhh_c3(_xhh_convert(value))


def _xhh_c1(value: int) -> int:
    return _xhh_c2(_xhh_c3(_xhh_convert(value)))


def _xhh_c0(value: int) -> int:
    return _xhh_c1(value) ^ _xhh_c2(value) ^ _xhh_c3(value)


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
    if source == "Bilibili":
        return _parse_bilibili_html_card(url, html_text, title=title, description=description, cover=cover)
    if _looks_like_error_title(title):
        title = ""

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


def _parse_bilibili_html_card(
    url: str,
    html_text: str,
    *,
    title: str,
    description: str,
    cover: str,
) -> CardContent:
    state = _extract_js_object(html_text, "window.__INITIAL_STATE__=")
    video = _first_dict(state, ("videoData",))
    owner = _first_dict(video, ("owner",))
    image_candidates = _unique_urls(
        _images_from_value(video.get("pic"))
        + _images_from_value(video.get("cover"))
        + _images_from_value(cover)
    )
    cover_url = next((item for item in image_candidates if not _is_bilibili_placeholder_image(item)), "")
    return CardContent(
        source="Bilibili",
        title=_clean_text(_first_value(video, ("title",)) or title, 180) or "Bilibili",
        url=url,
        description=_clean_text(_first_value(video, ("desc", "description")) or description, 600),
        cover_url=cover_url,
        author=_clean_text(_first_value(owner, ("name",)), 80),
        footer="Bilibili",
    )


def _looks_like_error_title(value: str) -> bool:
    text = _clean_text(value, 80)
    return bool(re.fullmatch(r"[45]\d{2}(?:\s+\w+)?", text, flags=re.IGNORECASE))


def _first_meta(soup: BeautifulSoup, names: list[str]) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return html.unescape(str(tag["content"]).strip())
    return ""


def _tag_text(tag) -> str:
    return tag.get_text(" ", strip=True) if tag else ""


def _nga_tag_text(tag) -> str:
    if not tag:
        return ""
    value = tag.get_text("\n", strip=True)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value


def _extract_js_object(html_text: str, marker: str) -> dict:
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
                    return data if isinstance(data, dict) else {}
                except Exception:
                    return {}
    return {}


def _is_bilibili_placeholder_image(url: str) -> bool:
    lowered = str(url or "").strip().lower()
    return not lowered or "transparent.png" in lowered


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
        raw_body = _nga_tag_text(content_tag)
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
        raw_body = _nga_tag_text(comment_tag)
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


def _nga_smiley_inline_token(value: str) -> str:
    url = _nga_smiley_url(value)
    if url:
        return f"{NGA_SMILEY_TOKEN_PREFIX}{url}{NGA_SMILEY_TOKEN_SUFFIX}"
    return _nga_smiley_label(value)


def _date_from_timestamp(value: int) -> str:
    if not value:
        return ""
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

    # ── preserve quote blocks: prefix each line with \x01 ──────────
    def _mark_quote_lines(match):
        inner = match.group(1)
        return "\n".join(
            "\x01" + line for line in inner.splitlines() if line.strip()
        )

    value = re.sub(
        r"\[quote(?:=[^\]]*)?\](.*?)\[/quote\]",
        _mark_quote_lines,
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # strip leftover orphan quote tags
    value = re.sub(r"\[/?quote(?:=[^\]]*)?\]", "\n", value, flags=re.IGNORECASE)

    replacements = [
        (r"\[br\]", "\n"),
        (r"\[img\].*?\[/img\]", ""),
        (r"\[s:([^\]]+)\]", lambda match: _nga_smiley_inline_token(match.group(1))),
        (r"\[url(?:=[^\]]+)?\](.*?)\[/url\]", r"\1"),
        (r"\[(?:/?)(?:h|align|size|b|i|u|color|collapse|del|font|list|\\*)[^\]]*\]", ""),
    ]
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"\[[a-zA-Z0-9_/*=\-:#%,.\s]+\]", "", value)
    value = re.sub(r"\s*\n\s*", "\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


def _clean_weibo_title(title: str) -> str:
    value = _clean_text(title, 180)
    value = re.sub(r"[\-_ ]*微博.*$", "", value)
    return value.strip()


def _clean_weibo_text(text: str) -> str:
    value = _clean_text(text, 520)
    value = re.sub(r"网页链接$", "", value).strip()
    return value
