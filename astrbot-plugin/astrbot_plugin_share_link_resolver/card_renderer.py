from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont, ImageOps


RESAMPLE_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
TEXT_TRACKING = 1

FONT_CANDIDATES = [
    "/AstrBot/data/plugins/astrbot_plugin_share_link_resolver/.local/fonts/SourceHanSansCN-Regular.ttc",
    "/AstrBot/data/plugins/astrbot_plugin_share_link_resolver/.local/fonts/SourceHanSansCN-Regular.ttf",
    "/AstrBot/data/plugins/astrbot_plugin_share_link_resolver/.local/fonts/SourceHanSansCN-Medium.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]
EMOJI_CACHE_DIR = Path(__file__).resolve().parent / ".local" / "emoji"
TWEMOJI_BASE_URL = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
NGA_SMILEY_TOKEN_RE = re.compile(r"\x1eNGA_SMILEY:(https?://[^\x1f]+)\x1f")


@dataclass(frozen=True)
class ThreadPost:
    floor: int
    author: str
    meta: str
    body: str
    title: str = ""
    likes: str = ""
    avatar_url: str = ""
    image_urls: tuple[str, ...] = ()
    smiley_urls: tuple[str, ...] = ()
    kind: str = ""


@dataclass(frozen=True)
class CardContent:
    source: str
    title: str
    url: str
    description: str = ""
    cover_url: str = ""
    author: str = ""
    footer: str = ""
    posts: tuple[ThreadPost, ...] = ()


def render_card(content: CardContent, output_dir: Path, *, width: int = 760, max_height: int = 2200) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        "|".join(
            [
                "card-v17",
                content.source,
                content.title,
                content.url,
                content.description,
                content.cover_url,
                str(len(content.posts)),
                "|".join(
                    post.author
                    + post.meta
                    + post.body[:80]
                    + post.avatar_url
                    + post.kind
                    + post.likes
                    + "|".join(post.image_urls)
                    + "|".join(post.smiley_urls)
                    for post in content.posts
                ),
            ]
        ).encode("utf-8")
    ).hexdigest()[:24]
    output_path = output_dir / f"{digest}.png"
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    if content.source == "NGA" and content.posts:
        _render_nga_thread(content, output_path, width=width, max_height=max_height)
        return output_path
    if content.posts and content.posts[0].kind == "article":
        _render_article_card(content, output_path, width=width, max_height=max_height)
        return output_path

    palette = _palette_for_source(content.source)
    margin = 22
    padding = 22
    card_width = width - margin * 2
    avatar_size = 52
    text_offset = avatar_size + 14
    content_width = card_width - padding * 2
    post_text_width = content_width - text_offset

    fonts = {
        "source": _font(18),
        "title": _font(27, bold=True),
        "body": _font(21),
        "muted": _font(16),
        "url": _font(15),
    }

    cover = _load_cover(content.cover_url, content_width)
    title_lines = _wrap_text(content.title or "未获取到标题", fonts["title"], content_width)
    desc_lines = _wrap_text(content.description, fonts["body"], content_width)[:10]
    url_lines = _wrap_text(_compact_url(content.url), fonts["url"], content_width)[:3]

    y = margin + padding
    height = y
    height += _line_height(fonts["source"]) + 18
    height += sum(_line_height(fonts["title"]) + 6 for _ in title_lines) + 12
    if content.author:
        height += _line_height(fonts["muted"]) + 16
    if cover:
        height += cover.height + 18
    if desc_lines:
        height += sum(_line_height(fonts["body"]) + 8 for _ in desc_lines) + 14
    height += 1 + 16
    height += sum(_line_height(fonts["url"]) + 5 for _ in url_lines) + 18
    if content.footer:
        height += _line_height(fonts["muted"]) + 8
    height += padding + margin
    height = min(max(height, 360), max_height)

    image = Image.new("RGB", (width, height), palette["bg"])
    draw = ImageDraw.Draw(image)
    _rounded_rect(draw, (margin, margin, width - margin, height - margin), 8, palette["card"], palette["line"])

    y = margin + padding
    source = content.source or _host_label(content.url)
    _draw_text(image, draw, (margin + padding, y), source, fill=palette["accent"], font=fonts["source"])
    y += _line_height(fonts["source"]) + 16
    draw.line((margin + padding, y, width - margin - padding, y), fill=palette["line"], width=1)
    y += 18

    for line in title_lines:
        _draw_text(image, draw, (margin + padding, y), line, fill=palette["accent"], font=fonts["title"])
        y += _line_height(fonts["title"]) + 6
    y += 10

    if content.author:
        _draw_text(image, draw, (margin + padding, y), content.author, fill=palette["muted"], font=fonts["muted"])
        y += _line_height(fonts["muted"]) + 16

    if cover:
        image.paste(cover, (margin + padding, y))
        y += cover.height + 18

    if desc_lines:
        for line in desc_lines:
            _draw_text(image, draw, (margin + padding, y), line, fill=palette["text"], font=fonts["body"])
            y += _line_height(fonts["body"]) + 8
        y += 8

    draw.line((margin + padding, y, width - margin - padding, y), fill=palette["line"], width=1)
    y += 16
    for line in url_lines:
        _draw_text(image, draw, (margin + padding, y), line, fill=palette["muted"], font=fonts["url"])
        y += _line_height(fonts["url"]) + 5

    if content.footer:
        footer_y = min(y + 8, height - margin - padding - _line_height(fonts["muted"]))
        _draw_text(image, draw, (margin + padding, footer_y), content.footer, fill=palette["muted"], font=fonts["muted"])

    image.save(output_path, "PNG", optimize=True)
    return output_path


def _render_article_card(content: CardContent, output_path: Path, *, width: int, max_height: int) -> None:
    palette = _palette_for_source(content.source)
    margin = 22
    padding = 24
    card_width = width - margin * 2
    content_width = card_width - padding * 2
    fonts = {
        "source": _font(18),
        "title": _font(30, bold=True),
        "author": _font(18, bold=True),
        "meta": _font(15),
        "body": _font(21),
        "url": _font(15),
        "small": _font(14),
    }
    post = content.posts[0]
    title_lines = _wrap_text(content.title or post.title or "未获取到标题", fonts["title"], content_width)[:4]
    body_lines = _wrap_article_text(post.body or content.description, fonts["body"], content_width, max_lines=32)
    image_urls = post.image_urls or ((content.cover_url,) if content.cover_url else ())
    post_images = _load_post_images(tuple(image_urls), content_width, limit=5, max_image_height=None)
    url_lines = _wrap_text(_compact_url(content.url), fonts["url"], content_width)[:3]
    footer_height = 1 + 14 + sum(_line_height(fonts["url"]) + 5 for _ in url_lines)

    height = margin + padding
    height += _line_height(fonts["source"]) + 16 + 1 + 18
    height += sum(_line_height(fonts["title"]) + 7 for _ in title_lines) + 12
    height += max(52, _line_height(fonts["author"]) + _line_height(fonts["meta"]) + 10) + 20
    height += sum((_line_height(fonts["body"]) + 8) if line else 14 for line in body_lines) + 14
    height += sum(image.height + 16 for image in post_images)
    height += footer_height + padding + margin
    height = min(max(height, 420), max_height)

    image = Image.new("RGB", (width, height), palette["bg"])
    draw = ImageDraw.Draw(image)
    _rounded_rect(draw, (margin, margin, width - margin, height - margin), 8, palette["card"], palette["line"])

    x = margin + padding
    y = margin + padding
    _draw_text(image, draw, (x, y), content.footer or content.source or _host_label(content.url), fill=palette["accent"], font=fonts["source"])
    y += _line_height(fonts["source"]) + 16
    draw.line((x, y, width - margin - padding, y), fill=palette["line"], width=1)
    y += 18

    for line in title_lines:
        _draw_text(image, draw, (x, y), line, fill=palette["accent"], font=fonts["title"])
        y += _line_height(fonts["title"]) + 7
    y += 12

    _draw_avatar(image, draw, post.avatar_url, (x, y), 48, palette, fallback=post.author or content.source)
    author_x = x + 62
    _draw_text(image, draw, (author_x, y + 1), post.author or content.author or content.source, fill=palette["text"], font=fonts["author"])
    meta = post.meta or content.author
    if meta:
        _draw_text(image, draw, (author_x, y + _line_height(fonts["author"]) + 5), meta, fill=palette["muted"], font=fonts["meta"])
    y += 68

    for line in body_lines:
        if y > height - margin - padding - 120:
            _draw_text(image, draw, (x, y), "内容过长，已截断，打开链接查看。", fill=palette["muted"], font=fonts["small"])
            y += _line_height(fonts["small"]) + 12
            break
        if not line:
            y += 14
            continue
        _draw_text(image, draw, (x, y), line, fill=palette["text"], font=fonts["body"])
        y += _line_height(fonts["body"]) + 8
    y += 8

    for post_image in post_images:
        if y + post_image.height > height - margin - padding - footer_height:
            break
        image.paste(post_image, (x, y))
        y += post_image.height + 16

    y = min(y, height - margin - padding - footer_height)
    draw.line((x, y, width - margin - padding, y), fill=palette["line"], width=1)
    y += 14
    for line in url_lines:
        _draw_text(image, draw, (x, y), line, fill=palette["muted"], font=fonts["url"])
        y += _line_height(fonts["url"]) + 5

    image = _trim_card_bottom(image, width, y + padding + margin, margin, palette)
    image.save(output_path, "PNG", optimize=True)


def _render_nga_thread(content: CardContent, output_path: Path, *, width: int, max_height: int) -> None:
    palette = _palette_for_source("NGA")
    margin = 20
    padding = 20
    gap = 16
    card_width = width - margin * 2
    content_width = card_width - padding * 2
    reply_text_width = content_width - 60
    fonts = {
        "source": _font(17),
        "title": _font(26, bold=True),
        "author": _font(19, bold=True),
        "meta": _font(15),
        "body": _font(20),
        "small": _font(14),
        "section": _font(22, bold=True),
    }

    posts = content.posts[:5]
    main_post = posts[0]
    reply_posts = posts[1:]
    reply_section_title = "热门回复" if any(post.kind == "hot" for post in reply_posts) else "回复预览"
    title_lines = _wrap_text(content.title or main_post.title or "NGA 主题", fonts["title"], content_width)
    main_lines = _wrap_rich_text(main_post.body, fonts["body"], content_width, max_lines=18)
    main_smileys = [] if _has_nga_smiley_tokens(main_post.body) else _load_smiley_images(main_post.smiley_urls, limit=4)
    main_images = _load_post_images(main_post.image_urls, content_width, limit=2, max_image_height=380)
    reply_line_groups = [
        (
            _wrap_rich_text(post.body, fonts["body"], reply_text_width, max_lines=6),
            post,
            [] if _has_nga_smiley_tokens(post.body) else _load_smiley_images(post.smiley_urls, limit=3),
            _load_post_images(post.image_urls, reply_text_width, limit=2, max_image_height=220),
        )
        for post in reply_posts
    ]

    height = margin + padding
    height += _line_height(fonts["source"]) + 16 + 1 + 16
    height += sum(_line_height(fonts["title"]) + 7 for _ in title_lines) + 12 + 1 + 16
    height += _line_height(fonts["author"]) + _line_height(fonts["meta"]) + 18
    height += _rich_lines_height(main_lines, fonts["body"], 8) + 20 + 1 + _line_height(fonts["small"]) + padding
    height += _smiley_row_height(main_smileys) + (12 if main_smileys else 0)
    height += sum(post_image.height + 12 for post_image in main_images)
    if reply_line_groups:
        height += gap + _line_height(fonts["section"]) + 12
        for lines, _post, smileys, post_images in reply_line_groups:
            height += padding + _line_height(fonts["author"]) + _line_height(fonts["meta"]) + 14
            height += _rich_lines_height(lines, fonts["body"], 8) + 14 + _line_height(fonts["small"]) + padding + gap
            height += _smiley_row_height(smileys) + (12 if smileys else 0)
            height += sum(post_image.height + 12 for post_image in post_images)
        if len(reply_line_groups) >= 4:
            height += 180
    height = min(max(height + margin, 520), max_height)

    image = Image.new("RGB", (width, height), palette["bg"])
    draw = ImageDraw.Draw(image)
    y = margin

    main_card_bottom = _draw_nga_main_card(
        image,
        draw,
        (margin, y, width - margin, height - margin),
        content,
        main_post,
        title_lines,
        main_lines,
        main_smileys,
        main_images,
        fonts,
        palette,
        padding,
    )
    y = main_card_bottom + gap

    if reply_line_groups and y < height - margin:
        _draw_text(image, draw, (margin, y), reply_section_title, fill=palette["accent"], font=fonts["section"])
        y += _line_height(fonts["section"]) + 12
        for lines, post, smileys, post_images in reply_line_groups:
            if y > height - 160:
                break
            y = _draw_nga_reply_card(
                image,
                draw,
                (margin, y, width - margin, height - margin),
                post,
                lines,
                smileys,
                post_images,
                fonts,
                palette,
                padding,
            ) + gap

    image.save(output_path, "PNG", optimize=True)


def _draw_nga_main_card(image, draw, box, content, post, title_lines, body_lines, smileys, post_images, fonts, palette, padding: int) -> int:
    left, top, right, _bottom = box
    x = left + padding
    y = top + padding
    _rounded_rect(draw, (left, top, right, top + 9999), 6, palette["card"], palette["line"])
    source = content.footer or "NGA"
    _draw_text(image, draw, (x, y), source, fill=palette["accent"], font=fonts["source"])
    y += _line_height(fonts["source"]) + 16
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 16
    for line in title_lines:
        _draw_text(image, draw, (x, y), line, fill=palette["accent"], font=fonts["title"])
        y += _line_height(fonts["title"]) + 7
    y += 10
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 16
    _draw_avatar(image, draw, post.avatar_url, (x, y), 54, palette, fallback=(post.author or "楼"))
    author_x = x + 68
    _draw_text(image, draw, (author_x, y + 2), post.author or "楼主", fill=palette["accent"], font=fonts["author"])
    y += _line_height(fonts["author"]) + 4
    if post.meta:
        _draw_text(image, draw, (author_x, y), post.meta, fill=palette["muted"], font=fonts["meta"])
        y += _line_height(fonts["meta"]) + 18
    y = max(y, top + padding + 62)
    y = _draw_rich_lines(image, draw, x, y, body_lines, fill=palette["text"], font=fonts["body"], gap=8)
    if smileys:
        y = _draw_smiley_row(image, smileys, x, y) + 12
    for post_image in post_images:
        image.paste(post_image, (x, y))
        y += post_image.height + 12
    y += 10
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 12
    _draw_text(image, draw, (x, y), _compact_url(content.url), fill=palette["muted"], font=fonts["small"])
    y += _line_height(fonts["small"]) + padding
    draw.rectangle((left, y, right, top + 9999), fill=palette["bg"])
    _rounded_rect(draw, (left, top, right, y), 6, palette["card"], palette["line"])
    _redraw_nga_main_content(image, draw, (left, top, right, y), content, post, title_lines, body_lines, smileys, post_images, fonts, palette, padding)
    return y


def _redraw_nga_main_content(image, draw, box, content, post, title_lines, body_lines, smileys, post_images, fonts, palette, padding: int) -> None:
    left, top, right, _bottom = box
    x = left + padding
    y = top + padding
    _draw_text(image, draw, (x, y), content.footer or "NGA", fill=palette["accent"], font=fonts["source"])
    y += _line_height(fonts["source"]) + 16
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 16
    for line in title_lines:
        _draw_text(image, draw, (x, y), line, fill=palette["accent"], font=fonts["title"])
        y += _line_height(fonts["title"]) + 7
    y += 10
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 16
    _draw_avatar(image, draw, post.avatar_url, (x, y), 54, palette, fallback=(post.author or "楼"))
    author_x = x + 68
    _draw_text(image, draw, (author_x, y + 2), post.author or "楼主", fill=palette["accent"], font=fonts["author"])
    y += _line_height(fonts["author"]) + 4
    if post.meta:
        _draw_text(image, draw, (author_x, y), post.meta, fill=palette["muted"], font=fonts["meta"])
        y += _line_height(fonts["meta"]) + 18
    y = max(y, top + padding + 62)
    y = _draw_rich_lines(image, draw, x, y, body_lines, fill=palette["text"], font=fonts["body"], gap=8)
    if smileys:
        y = _draw_smiley_row(image, smileys, x, y) + 12
    for post_image in post_images:
        image.paste(post_image, (x, y))
        y += post_image.height + 12
    y += 10
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 12
    _draw_text(image, draw, (x, y), _compact_url(content.url), fill=palette["muted"], font=fonts["small"])


def _draw_nga_reply_card(image, draw, box, post, body_lines, smileys, post_images, fonts, palette, padding: int) -> int:
    left, top, right, _bottom = box
    x = left + padding
    y = top + padding
    height = padding + max(54, _line_height(fonts["author"]) + _line_height(fonts["meta"]) + 8) + 14
    height += _rich_lines_height(body_lines, fonts["body"], 8) + 14 + _line_height(fonts["small"]) + padding
    height += _smiley_row_height(smileys) + (12 if smileys else 0)
    height += sum(post_image.height + 12 for post_image in post_images)
    bottom = top + height
    _rounded_rect(draw, (left, top, right, bottom), 6, palette["card"], palette["line"])
    _draw_avatar(image, draw, post.avatar_url, (x, y), 46, palette, fallback=str(post.floor))
    author_x = x + 60
    _draw_text(image, draw, (author_x, y), post.author or f"{post.floor} 楼", fill=palette["accent"], font=fonts["author"])
    y += _line_height(fonts["author"]) + 4
    if post.meta:
        _draw_text(image, draw, (author_x, y), post.meta, fill=palette["muted"], font=fonts["meta"])
        y += _line_height(fonts["meta"]) + 14
    y = max(y, top + padding + 54)
    draw.line((left, y, right, y), fill=palette["line"], width=1)
    y += 14
    y = _draw_rich_lines(image, draw, x, y, body_lines, fill=palette["text"], font=fonts["body"], gap=8)
    if smileys:
        y = _draw_smiley_row(image, smileys, x, y) + 12
    for post_image in post_images:
        image.paste(post_image, (x, y))
        y += post_image.height + 12
    footer = post.likes if post.kind == "hot" else f"{post.floor} 楼"
    if post.likes and post.kind != "hot":
        footer = f"{post.likes}  {footer}"
    _draw_text(image, draw, (right - padding - _text_width(draw, footer, fonts["small"]), bottom - padding - _line_height(fonts["small"])), footer, fill=palette["muted"], font=fonts["small"])
    return bottom


def _load_post_images(urls: tuple[str, ...], max_width: int, *, limit: int, max_image_height: int | None) -> list[Image.Image]:
    images: list[Image.Image] = []
    for url in urls[:limit]:
        image = _load_cover(url, max_width)
        if not image:
            continue
        if max_image_height and image.height > max_image_height:
            ratio = max_image_height / image.height
            target_width = min(max_width, max(1, int(image.width * ratio)))
            image = image.resize((target_width, max_image_height), RESAMPLE_LANCZOS)
        images.append(image)
    return images


def _load_smiley_images(urls: tuple[str, ...], *, limit: int) -> list[Image.Image]:
    images: list[Image.Image] = []
    for url in urls[:limit]:
        image = _load_transparent_image(url, max_bytes=600_000)
        if not image:
            continue
        image.thumbnail((58, 58), RESAMPLE_LANCZOS)
        images.append(image)
    return images


def _smiley_row_height(images: list[Image.Image]) -> int:
    return max((image.height for image in images), default=0)


def _draw_smiley_row(canvas: Image.Image, images: list[Image.Image], x: int, y: int) -> int:
    cursor = x
    row_height = _smiley_row_height(images)
    for image in images:
        canvas.paste(image, (cursor, y), image if image.mode == "RGBA" else None)
        cursor += image.width + 8
    return y + row_height


def _has_nga_smiley_tokens(text: str) -> bool:
    return bool(NGA_SMILEY_TOKEN_RE.search(str(text or "")))


def _wrap_rich_text(text: str, font, max_width: int, *, max_lines: int) -> list[list[tuple[str, str]]]:
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines: list[list[tuple[str, str]]] = []
    paragraphs = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for paragraph in paragraphs:
        paragraph = re.sub(r"[ \t\f\v]+", " ", paragraph).strip()
        if not paragraph:
            if lines and lines[-1]:
                lines.append([])
            if len(lines) >= max_lines:
                return lines[:max_lines]
            continue
        current: list[tuple[str, str]] = []
        current_width = 0.0
        for unit in _iter_rich_units(paragraph):
            unit_width = _rich_unit_width(draw, unit, font)
            if current and current_width + unit_width > max_width:
                lines.append(current)
                if len(lines) >= max_lines:
                    return lines[:max_lines]
                current = [unit]
                current_width = unit_width
            else:
                current.append(unit)
                current_width += unit_width
        if current:
            lines.append(current)
            if len(lines) >= max_lines:
                return lines[:max_lines]
    while lines and not lines[-1]:
        lines.pop()
    return lines[:max_lines]


def _iter_rich_units(text: str):
    index = 0
    value = str(text or "")
    while index < len(value):
        match = NGA_SMILEY_TOKEN_RE.match(value, index)
        if match:
            yield ("image", match.group(1))
            index = match.end()
            continue
        emoji, next_index = _emoji_sequence_at(value, index)
        if emoji:
            yield ("text", emoji)
            index = next_index
        else:
            yield ("text", value[index])
            index += 1


def _rich_unit_width(draw: ImageDraw.ImageDraw, unit: tuple[str, str], font) -> float:
    kind, value = unit
    if kind == "image":
        return _nga_inline_smiley_size(font) + 4
    return _text_width(draw, value, font) + TEXT_TRACKING


def _rich_lines_height(lines: list[list[tuple[str, str]]], font, gap: int) -> int:
    total = 0
    for line in lines:
        if not line:
            total += max(8, _line_height(font) // 2) + gap
            continue
        total += max(_line_height(font), _nga_inline_smiley_size(font)) + gap
    return total


def _draw_rich_lines(canvas: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int, lines: list[list[tuple[str, str]]], *, fill, font, gap: int) -> int:
    line_height = max(_line_height(font), _nga_inline_smiley_size(font))
    for line in lines:
        if not line:
            y += max(8, _line_height(font) // 2) + gap
            continue
        cursor = x
        for unit in line:
            kind, value = unit
            if kind == "image":
                smiley = _load_inline_smiley_image(value, _nga_inline_smiley_size(font))
                if smiley:
                    offset_y = y + max(0, (line_height - smiley.height) // 2)
                    canvas.paste(smiley, (int(cursor), int(offset_y)), smiley)
                    cursor += smiley.width + 4
                    continue
                fallback = "〔表情〕"
                _draw_text(canvas, draw, (cursor, y), fallback, fill=fill, font=font)
                cursor += _text_width(draw, fallback, font)
                continue
            _draw_text(canvas, draw, (cursor, y), value, fill=fill, font=font)
            cursor += _text_width(draw, value, font)
        y += line_height + gap
    return y


def _nga_inline_smiley_size(font) -> int:
    return max(22, min(34, int(_line_height(font) * 1.25)))


def _load_inline_smiley_image(url: str, size: int) -> Image.Image | None:
    image = _load_transparent_image(url, max_bytes=600_000)
    if not image:
        return None
    image.thumbnail((size, size), RESAMPLE_LANCZOS)
    return image


def _load_transparent_image(url: str, *, max_bytes: int) -> Image.Image | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    try:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 NightingaleOpsBot-ShareCard/0.1",
                "Referer": f"{parsed.scheme}://{parsed.netloc}/",
            },
        )
        with urlopen(request, timeout=8) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None
        with Image.open(io.BytesIO(data)) as source:
            return source.convert("RGBA")
    except (OSError, URLError, ValueError):
        return None


def _draw_avatar(canvas: Image.Image, draw: ImageDraw.ImageDraw, url: str, xy: tuple[int, int], size: int, palette, fallback: str = "") -> None:
    avatar = _load_square_image(url, size)
    x, y = xy
    if avatar:
        canvas.paste(avatar, (x, y), _circle_mask(size))
        return
    draw.ellipse((x, y, x + size, y + size), fill=(241, 235, 224), outline=palette["line"], width=1)
    letter = str(fallback or "U").strip()[:1].upper()
    font = _font(max(16, size // 3), bold=True)
    text_w = _text_width(draw, letter, font)
    text_h = _line_height(font)
    draw.text((x + (size - text_w) / 2, y + (size - text_h) / 2 - 1), letter, fill=palette["muted"], font=font)


def _circle_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    return mask


def _load_square_image(url: str, size: int) -> Image.Image | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    try:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 NightingaleOpsBot-ShareCard/0.1",
                "Referer": f"{parsed.scheme}://{parsed.netloc}/",
            },
        )
        with urlopen(request, timeout=8) as response:
            data = response.read(1_500_000 + 1)
        if len(data) > 1_500_000:
            return None
        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGB")
    except (OSError, URLError, ValueError):
        return None
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    return image.resize((size, size), RESAMPLE_LANCZOS)


def _font(size: int, *, bold: bool = False):
    if bold:
        bold_candidates = [path.replace("Regular", "Bold").replace("Medium", "Bold") for path in FONT_CANDIDATES]
        for path in bold_candidates:
            font = _try_font(path, size)
            if font:
                return font
    for path in FONT_CANDIDATES:
        font = _try_font(path, size)
        if font:
            return font
    return ImageFont.load_default()


def _try_font(path: str, size: int):
    try:
        if Path(path).exists():
            if Path(path).suffix.lower() == ".ttc":
                for index in (0, 1, 2, 7):
                    try:
                        return ImageFont.truetype(path, size, index=index)
                    except Exception:
                        continue
            return ImageFont.truetype(path, size)
    except Exception:
        return None
    return None


def _line_height(font) -> int:
    if hasattr(font, "getbbox"):
        bbox = font.getbbox("夜莺Nightingale")
        return max(18, bbox[3] - bbox[1])
    if hasattr(font, "getsize"):
        return max(18, font.getsize("夜莺Nightingale")[1])
    return 24


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    if not value:
        return []
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines: list[str] = []
    current = ""
    for unit in _iter_text_units(value):
        trial = current + unit
        if _text_width(draw, trial, font) <= max_width or not current:
            current = trial
            continue
        lines.append(current)
        current = unit
    if current:
        lines.append(current)
    return lines


def _wrap_article_text(text: str, font, max_width: int, *, max_lines: int) -> list[str]:
    result: list[str] = []
    for paragraph in re.split(r"\n+", str(text or "").strip()):
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if not paragraph:
            continue
        if result:
            result.append("")
        result.extend(_wrap_text(paragraph, font, max_width))
        if len(result) >= max_lines:
            return result[:max_lines]
    return result


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    units = list(_iter_text_units(str(text or "")))
    if not units:
        return 0
    width = 0.0
    for unit in units:
        if _emoji_sequence_at(unit, 0)[0] == unit:
            width += _emoji_size_for_font(font) + 2
        else:
            width += _plain_text_width(draw, unit, font)
    return width + max(0, len(units) - 1) * TEXT_TRACKING


def _plain_text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    if hasattr(draw, "textlength"):
        return draw.textlength(text, font=font)
    if hasattr(draw, "textbbox"):
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    if hasattr(font, "getbbox"):
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]
    if hasattr(font, "getsize"):
        return font.getsize(text)[0]
    return len(text) * 12


def _draw_text(canvas: Image.Image, draw: ImageDraw.ImageDraw, xy, text: str, *, fill, font) -> None:
    x, y = xy
    cursor = x
    units = list(_iter_text_units(str(text or "")))
    for index, unit in enumerate(units):
        emoji_value, _next_index = _emoji_sequence_at(unit, 0)
        if emoji_value == unit:
            emoji = _load_emoji_image(unit, _emoji_size_for_font(font))
            if emoji:
                offset_y = y + max(0, (_line_height(font) - emoji.height) // 2)
                canvas.paste(emoji, (int(cursor), int(offset_y)), emoji)
                cursor += emoji.width + 2
                if index < len(units) - 1:
                    cursor += TEXT_TRACKING
                continue
        draw.text((cursor, y), unit, fill=fill, font=font)
        cursor += _plain_text_width(draw, unit, font)
        if index < len(units) - 1:
            cursor += TEXT_TRACKING


def _trim_card_bottom(image: Image.Image, width: int, desired_height: int, margin: int, palette: dict[str, tuple[int, int, int]]) -> Image.Image:
    final_height = max(420, min(image.height, desired_height))
    if final_height >= image.height:
        return image
    trimmed = image.crop((0, 0, width, final_height))
    draw = ImageDraw.Draw(trimmed)
    try:
        draw.rounded_rectangle((margin, margin, width - margin, final_height - margin), radius=8, outline=palette["line"], width=1)
    except AttributeError:
        draw.rectangle((margin, margin, width - margin, final_height - margin), outline=palette["line"])
    return trimmed


def _iter_text_units(text: str):
    index = 0
    while index < len(text):
        emoji, next_index = _emoji_sequence_at(text, index)
        if emoji:
            yield emoji
            index = next_index
        else:
            yield text[index]
            index += 1


def _text_segments(text: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    buffer: list[str] = []
    index = 0
    while index < len(text):
        emoji, next_index = _emoji_sequence_at(text, index)
        if emoji:
            if buffer:
                segments.append(("text", "".join(buffer)))
                buffer = []
            segments.append(("emoji", emoji))
            index = next_index
            continue
        buffer.append(text[index])
        index += 1
    if buffer:
        segments.append(("text", "".join(buffer)))
    return segments


def _emoji_sequence_at(text: str, index: int) -> tuple[str, int]:
    if index >= len(text) or not _is_emoji_base(text[index]):
        return "", index
    parts = [text[index]]
    index += 1
    while index < len(text):
        char = text[index]
        if _is_emoji_modifier(char):
            parts.append(char)
            index += 1
            continue
        if char == "\u200d" and index + 1 < len(text) and _is_emoji_base(text[index + 1]):
            parts.extend([char, text[index + 1]])
            index += 2
            continue
        break
    return "".join(parts), index


def _is_emoji_base(char: str) -> bool:
    codepoint = ord(char)
    if 0x1F000 <= codepoint <= 0x1FAFF:
        return True
    if 0x2600 <= codepoint <= 0x27BF:
        return unicodedata.category(char) in {"So", "Sk"} or codepoint in {0x2764, 0x263A}
    return False


def _is_emoji_modifier(char: str) -> bool:
    codepoint = ord(char)
    return (
        codepoint == 0xFE0F
        or codepoint == 0xFE0E
        or codepoint == 0x20E3
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or 0xE0020 <= codepoint <= 0xE007F
    )


def _emoji_size_for_font(font) -> int:
    return max(18, min(72, int(_line_height(font) * 1.05)))


def _emoji_filename(emoji: str) -> str:
    return "-".join(f"{ord(char):x}" for char in emoji if ord(char) != 0xFE0E)


def _emoji_filenames(emoji: str) -> list[str]:
    candidates = [_emoji_filename(emoji)]
    without_variation = "-".join(f"{ord(char):x}" for char in emoji if ord(char) not in {0xFE0E, 0xFE0F})
    if without_variation and without_variation not in candidates:
        candidates.append(without_variation)
    return [candidate for candidate in candidates if candidate]


def _load_emoji_image(emoji: str, size: int) -> Image.Image | None:
    filenames = _emoji_filenames(emoji)
    if not filenames:
        return None
    EMOJI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for filename in filenames:
        cache_path = EMOJI_CACHE_DIR / f"{filename}.png"
        if not cache_path.exists() or cache_path.stat().st_size <= 0:
            url = f"{TWEMOJI_BASE_URL}/{filename}.png"
            try:
                request = Request(url, headers={"User-Agent": "Mozilla/5.0 NightingaleOpsBot-ShareCard/0.1"})
                with urlopen(request, timeout=8) as response:
                    data = response.read(400_000 + 1)
                if len(data) > 400_000:
                    continue
                cache_path.write_bytes(data)
            except (OSError, URLError, ValueError):
                continue
        try:
            with Image.open(cache_path) as source:
                image = source.convert("RGBA")
            return image.resize((size, size), RESAMPLE_LANCZOS)
        except (OSError, ValueError):
            continue
    return None


def _rounded_rect(draw: ImageDraw.ImageDraw, box, radius: int, fill, outline) -> None:
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)
    except Exception:
        draw.rectangle(box, fill=fill, outline=outline)


def _load_cover(url: str, max_width: int) -> Image.Image | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    try:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 NightingaleOpsBot-ShareCard/0.1",
                "Referer": f"{parsed.scheme}://{parsed.netloc}/",
            },
        )
        with urlopen(request, timeout=12) as response:
            data = response.read(5_000_000 + 1)
        if len(data) > 5_000_000:
            return None
        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGB")
    except (OSError, URLError, ValueError):
        return None

    bordered_width = max(1, max_width - 2)
    if image.width != bordered_width:
        target_height = max(1, round(image.height * (bordered_width / image.width)))
        image = image.resize((bordered_width, target_height), RESAMPLE_LANCZOS)
    return ImageOps.expand(image, border=1, fill=(225, 201, 159))


def _compact_url(url: str) -> str:
    value = str(url or "").strip()
    return value if len(value) <= 220 else value[:217] + "..."


def _host_label(url: str) -> str:
    host = urlparse(url).hostname or "网页"
    return host.removeprefix("www.")


def _palette_for_source(source: str) -> dict[str, tuple[int, int, int]]:
    normalized = str(source or "").lower()
    if "小红书" in source or "xiaohongshu" in normalized:
        return {
            "bg": (255, 244, 246),
            "card": (255, 255, 255),
            "line": (245, 178, 186),
            "text": (37, 43, 51),
            "muted": (128, 112, 116),
            "accent": (198, 28, 47),
        }
    if "微博" in source or "weibo" in normalized:
        return {
            "bg": (255, 246, 235),
            "card": (255, 255, 255),
            "line": (244, 193, 126),
            "text": (43, 48, 56),
            "muted": (126, 115, 103),
            "accent": (225, 95, 20),
        }
    if "米游社" in source or "miyoushe" in normalized:
        return {
            "bg": (246, 248, 252),
            "card": (255, 255, 255),
            "line": (214, 224, 239),
            "text": (31, 38, 48),
            "muted": (120, 132, 148),
            "accent": (48, 111, 191),
        }
    if "taptap" in normalized:
        return {
            "bg": (239, 252, 253),
            "card": (255, 255, 255),
            "line": (154, 224, 230),
            "text": (28, 46, 52),
            "muted": (91, 124, 132),
            "accent": (0, 159, 174),
        }
    if "库街区" in source or "kurobbs" in normalized:
        return {
            "bg": (238, 250, 251),
            "card": (255, 255, 255),
            "line": (151, 219, 226),
            "text": (25, 43, 50),
            "muted": (88, 119, 128),
            "accent": (0, 145, 166),
        }
    if "小黑盒" in source or "xiaoheihe" in normalized or "heybox" in normalized:
        return {
            "bg": (244, 246, 249),
            "card": (255, 255, 255),
            "line": (200, 207, 216),
            "text": (24, 27, 32),
            "muted": (104, 113, 125),
            "accent": (20, 24, 31),
        }
    if "NGA" in source.upper() or "ngabbs" in normalized:
        return {
            "bg": (255, 240, 205),
            "card": (255, 252, 245),
            "line": (222, 181, 111),
            "text": (27, 48, 66),
            "muted": (112, 119, 125),
            "accent": (112, 32, 13),
        }
    return {
        "bg": (246, 247, 249),
        "card": (255, 255, 255),
        "line": (210, 214, 220),
        "text": (25, 28, 33),
        "muted": (105, 113, 125),
        "accent": (18, 20, 24),
    }
