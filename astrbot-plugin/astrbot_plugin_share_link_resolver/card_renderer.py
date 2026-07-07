from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont, ImageOps


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
                "card-v11",
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
    draw.text((margin + padding, y), source, fill=palette["accent"], font=fonts["source"])
    y += _line_height(fonts["source"]) + 16
    draw.line((margin + padding, y, width - margin - padding, y), fill=palette["line"], width=1)
    y += 18

    for line in title_lines:
        draw.text((margin + padding, y), line, fill=palette["accent"], font=fonts["title"])
        y += _line_height(fonts["title"]) + 6
    y += 10

    if content.author:
        draw.text((margin + padding, y), content.author, fill=palette["muted"], font=fonts["muted"])
        y += _line_height(fonts["muted"]) + 16

    if cover:
        image.paste(cover, (margin + padding, y))
        y += cover.height + 18

    if desc_lines:
        for line in desc_lines:
            draw.text((margin + padding, y), line, fill=palette["text"], font=fonts["body"])
            y += _line_height(fonts["body"]) + 8
        y += 8

    draw.line((margin + padding, y, width - margin - padding, y), fill=palette["line"], width=1)
    y += 16
    for line in url_lines:
        draw.text((margin + padding, y), line, fill=palette["muted"], font=fonts["url"])
        y += _line_height(fonts["url"]) + 5

    if content.footer:
        footer_y = min(y + 8, height - margin - padding - _line_height(fonts["muted"]))
        draw.text((margin + padding, footer_y), content.footer, fill=palette["muted"], font=fonts["muted"])

    image.save(output_path, "PNG", optimize=True)
    return output_path


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
    main_lines = _wrap_text(main_post.body, fonts["body"], content_width)[:18]
    main_smileys = _load_smiley_images(main_post.smiley_urls, limit=4)
    main_images = _load_post_images(main_post.image_urls, content_width, limit=2, max_image_height=380)
    reply_line_groups = [
        (
            _wrap_text(post.body, fonts["body"], reply_text_width)[:6],
            post,
            _load_smiley_images(post.smiley_urls, limit=3),
            _load_post_images(post.image_urls, reply_text_width, limit=2, max_image_height=220),
        )
        for post in reply_posts
    ]

    height = margin + padding
    height += _line_height(fonts["source"]) + 16 + 1 + 16
    height += sum(_line_height(fonts["title"]) + 7 for _ in title_lines) + 12 + 1 + 16
    height += _line_height(fonts["author"]) + _line_height(fonts["meta"]) + 18
    height += sum(_line_height(fonts["body"]) + 8 for _ in main_lines) + 20 + 1 + _line_height(fonts["small"]) + padding
    height += _smiley_row_height(main_smileys) + (12 if main_smileys else 0)
    height += sum(post_image.height + 12 for post_image in main_images)
    if reply_line_groups:
        height += gap + _line_height(fonts["section"]) + 12
        for lines, _post, smileys, post_images in reply_line_groups:
            height += padding + _line_height(fonts["author"]) + _line_height(fonts["meta"]) + 14
            height += sum(_line_height(fonts["body"]) + 8 for _ in lines) + 14 + _line_height(fonts["small"]) + padding + gap
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
        draw.text((margin, y), reply_section_title, fill=palette["accent"], font=fonts["section"])
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
    draw.text((x, y), source, fill=palette["accent"], font=fonts["source"])
    y += _line_height(fonts["source"]) + 16
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 16
    for line in title_lines:
        draw.text((x, y), line, fill=palette["accent"], font=fonts["title"])
        y += _line_height(fonts["title"]) + 7
    y += 10
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 16
    _draw_avatar(image, draw, post.avatar_url, (x, y), 54, palette, fallback=(post.author or "楼"))
    author_x = x + 68
    draw.text((author_x, y + 2), post.author or "楼主", fill=palette["accent"], font=fonts["author"])
    y += _line_height(fonts["author"]) + 4
    if post.meta:
        draw.text((author_x, y), post.meta, fill=palette["muted"], font=fonts["meta"])
        y += _line_height(fonts["meta"]) + 18
    y = max(y, top + padding + 62)
    for line in body_lines:
        draw.text((x, y), line, fill=palette["text"], font=fonts["body"])
        y += _line_height(fonts["body"]) + 8
    if smileys:
        y = _draw_smiley_row(image, smileys, x, y) + 12
    for post_image in post_images:
        image.paste(post_image, (x, y))
        y += post_image.height + 12
    y += 10
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 12
    draw.text((x, y), _compact_url(content.url), fill=palette["muted"], font=fonts["small"])
    y += _line_height(fonts["small"]) + padding
    draw.rectangle((left, y, right, top + 9999), fill=palette["bg"])
    _rounded_rect(draw, (left, top, right, y), 6, palette["card"], palette["line"])
    _redraw_nga_main_content(image, draw, (left, top, right, y), content, post, title_lines, body_lines, smileys, post_images, fonts, palette, padding)
    return y


def _redraw_nga_main_content(image, draw, box, content, post, title_lines, body_lines, smileys, post_images, fonts, palette, padding: int) -> None:
    left, top, right, _bottom = box
    x = left + padding
    y = top + padding
    draw.text((x, y), content.footer or "NGA", fill=palette["accent"], font=fonts["source"])
    y += _line_height(fonts["source"]) + 16
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 16
    for line in title_lines:
        draw.text((x, y), line, fill=palette["accent"], font=fonts["title"])
        y += _line_height(fonts["title"]) + 7
    y += 10
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 16
    _draw_avatar(image, draw, post.avatar_url, (x, y), 54, palette, fallback=(post.author or "楼"))
    author_x = x + 68
    draw.text((author_x, y + 2), post.author or "楼主", fill=palette["accent"], font=fonts["author"])
    y += _line_height(fonts["author"]) + 4
    if post.meta:
        draw.text((author_x, y), post.meta, fill=palette["muted"], font=fonts["meta"])
        y += _line_height(fonts["meta"]) + 18
    y = max(y, top + padding + 62)
    for line in body_lines:
        draw.text((x, y), line, fill=palette["text"], font=fonts["body"])
        y += _line_height(fonts["body"]) + 8
    if smileys:
        y = _draw_smiley_row(image, smileys, x, y) + 12
    for post_image in post_images:
        image.paste(post_image, (x, y))
        y += post_image.height + 12
    y += 10
    draw.line((x, y, right - padding, y), fill=palette["line"], width=1)
    y += 12
    draw.text((x, y), _compact_url(content.url), fill=palette["muted"], font=fonts["small"])


def _draw_nga_reply_card(image, draw, box, post, body_lines, smileys, post_images, fonts, palette, padding: int) -> int:
    left, top, right, _bottom = box
    x = left + padding
    y = top + padding
    height = padding + max(54, _line_height(fonts["author"]) + _line_height(fonts["meta"]) + 8) + 14
    height += sum(_line_height(fonts["body"]) + 8 for _ in body_lines) + 14 + _line_height(fonts["small"]) + padding
    height += _smiley_row_height(smileys) + (12 if smileys else 0)
    height += sum(post_image.height + 12 for post_image in post_images)
    bottom = top + height
    _rounded_rect(draw, (left, top, right, bottom), 6, palette["card"], palette["line"])
    _draw_avatar(image, draw, post.avatar_url, (x, y), 46, palette, fallback=str(post.floor))
    author_x = x + 60
    draw.text((author_x, y), post.author or f"{post.floor} 楼", fill=palette["accent"], font=fonts["author"])
    y += _line_height(fonts["author"]) + 4
    if post.meta:
        draw.text((author_x, y), post.meta, fill=palette["muted"], font=fonts["meta"])
        y += _line_height(fonts["meta"]) + 14
    y = max(y, top + padding + 54)
    draw.line((left, y, right, y), fill=palette["line"], width=1)
    y += 14
    for line in body_lines:
        draw.text((x, y), line, fill=palette["text"], font=fonts["body"])
        y += _line_height(fonts["body"]) + 8
    if smileys:
        y = _draw_smiley_row(image, smileys, x, y) + 12
    for post_image in post_images:
        image.paste(post_image, (x, y))
        y += post_image.height + 12
    footer = post.likes if post.kind == "hot" else f"{post.floor} 楼"
    if post.likes and post.kind != "hot":
        footer = f"{post.likes}  {footer}"
    draw.text((right - padding - _text_width(draw, footer, fonts["small"]), bottom - padding - _line_height(fonts["small"])), footer, fill=palette["muted"], font=fonts["small"])
    return bottom


def _load_post_images(urls: tuple[str, ...], max_width: int, *, limit: int, max_image_height: int) -> list[Image.Image]:
    images: list[Image.Image] = []
    for url in urls[:limit]:
        image = _load_cover(url, max_width)
        if not image:
            continue
        if image.height > max_image_height:
            ratio = max_image_height / image.height
            target_width = max(1, int(image.width * ratio))
            image = image.resize((target_width, max_image_height), Image.Resampling.LANCZOS)
        images.append(image)
    return images


def _load_smiley_images(urls: tuple[str, ...], *, limit: int) -> list[Image.Image]:
    images: list[Image.Image] = []
    for url in urls[:limit]:
        image = _load_transparent_image(url, max_bytes=600_000)
        if not image:
            continue
        image.thumbnail((58, 58), Image.Resampling.LANCZOS)
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
    return image.resize((size, size), Image.Resampling.LANCZOS)


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
                for index in (2, 0, 7):
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
    for char in value:
        trial = current + char
        if _text_width(draw, trial, font) <= max_width or not current:
            current = trial
            continue
        lines.append(current)
        current = char
    if current:
        lines.append(current)
    return lines


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
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

    image.thumbnail((max_width, 560), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (max_width, image.height), (246, 241, 232))
    canvas.paste(image, ((max_width - image.width) // 2, 0))
    return ImageOps.expand(canvas, border=1, fill=(225, 201, 159))


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
