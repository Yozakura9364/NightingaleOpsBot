from __future__ import annotations

import hashlib
import io
from pathlib import Path
import re
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont

from .feed_client import FeedItem


RESAMPLE_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
FONT_CANDIDATES = [
    "/AstrBot/data/plugins/astrbot_plugin_share_link_resolver/.local/fonts/SourceHanSansCN-Regular.ttc",
    "/AstrBot/data/plugins/astrbot_plugin_share_link_resolver/.local/fonts/SourceHanSansCN-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]
DISPLAY_REPLACEMENTS = {
    "🎁": "[抽奖]",
    "🔗": "[链接]",
    "❖": "*",
    "✦": "*",
    "✧": "*",
    "★": "*",
    "☆": "*",
    "▪": "-",
    "▫": "-",
    "•": "-",
}
UNSUPPORTED_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
)


def render_bili_card(
    item: FeedItem,
    output_dir: Path,
    *,
    image_path: str = "",
    width: int = 860,
    max_height: int = 2800,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        "|".join(
            [
                "bili-card-v1",
                item.item_id,
                item.title,
                item.link,
                item.published_at,
                item.summary,
                item.author_name,
                item.avatar_url,
                image_path,
            ]
        ).encode("utf-8")
    ).hexdigest()[:24]
    output_path = output_dir / f"{digest}.png"
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    bg = (243, 247, 251)
    card = (255, 255, 255)
    line = (221, 228, 236)
    accent = (251, 114, 153)
    text = (28, 32, 38)
    muted = (121, 130, 142)

    margin = 24
    padding = 28
    content_width = width - margin * 2 - padding * 2
    fonts = {
        "source": _font(18, bold=True),
        "author": _font(19, bold=True),
        "meta": _font(15),
        "title": _font(28, bold=True),
        "body": _font(21),
        "link": _font(14),
    }

    title = _normalize_display_text((item.title or _first_non_empty_line(item.summary) or "B站动态").strip())
    body = _normalize_display_text(_strip_leading_title(item.summary, title))
    main_image = _load_local_image(image_path, content_width, max_height=620)
    body_lines = _wrap_multiline(body, fonts["body"], content_width, max_lines=12 if main_image is not None else 28)
    link_lines = _wrap_multiline(_normalize_display_text(item.link), fonts["link"], content_width, max_lines=2)
    avatar = _load_remote_avatar(item.avatar_url, 54)

    height = margin + padding
    height += _line_height(fonts["source"]) + 18
    height += max(60, _line_height(fonts["author"]) + _line_height(fonts["meta"]) + 12) + 18
    height += sum(_line_height(fonts["title"]) + 8 for _ in _wrap_multiline(title, fonts["title"], content_width, max_lines=3)) + 8
    height += sum((_line_height(fonts["body"]) + 8) if line else 16 for line in body_lines) + 8
    if main_image is not None:
        height += main_image.height + 18
    height += 1 + 14
    height += sum(_line_height(fonts["link"]) + 5 for _ in link_lines)
    height += padding + margin
    height = min(max(height, 420), max_height)

    canvas = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(canvas)
    _rounded_rect(draw, (margin, margin, width - margin, height - margin), 10, card, line)

    x = margin + padding
    y = margin + padding
    _draw_text(draw, (x, y), "B站动态", accent, fonts["source"])
    y += _line_height(fonts["source"]) + 18

    if avatar is not None:
        canvas.paste(avatar, (x, y), _circle_mask(54))
    else:
        _draw_avatar_fallback(draw, x, y, 54, line, muted, item.author_name or "B")
    author_x = x + 70
    _draw_text(draw, (author_x, y + 1), _normalize_display_text(item.author_name or "B站用户"), text, fonts["author"])
    if item.published_at:
        _draw_text(draw, (author_x, y + _line_height(fonts["author"]) + 6), item.published_at, muted, fonts["meta"])
    y += 72

    title_lines = _wrap_multiline(title, fonts["title"], content_width, max_lines=3)
    for line_text in title_lines:
        _draw_text(draw, (x, y), line_text, text, fonts["title"])
        y += _line_height(fonts["title"]) + 8
    y += 4

    for line_text in body_lines:
        if not line_text:
            y += 16
            continue
        _draw_text(draw, (x, y), line_text, text, fonts["body"])
        y += _line_height(fonts["body"]) + 8
    y += 8

    if main_image is not None and y + main_image.height <= height - margin - padding - 48:
        canvas.paste(main_image, (x, y))
        y += main_image.height + 18

    draw.line((x, y, width - margin - padding, y), fill=line, width=1)
    y += 14
    for line_text in link_lines:
        _draw_text(draw, (x, y), line_text, muted, fonts["link"])
        y += _line_height(fonts["link"]) + 5

    canvas = canvas.crop((0, 0, width, min(height, y + padding + margin)))
    canvas.save(output_path, "PNG", optimize=True)
    return output_path


def _load_local_image(path: str, max_width: int, *, max_height: int) -> Image.Image | None:
    local = Path(str(path or "").strip())
    if not local.exists() or local.stat().st_size <= 0:
        return None
    try:
        with Image.open(local) as source:
            image = source.convert("RGB")
    except OSError:
        return None
    bordered_width = max(1, max_width - 2)
    if image.width != bordered_width:
        target_height = max(1, round(image.height * (bordered_width / image.width)))
        image = image.resize((bordered_width, target_height), RESAMPLE_LANCZOS)
    if image.height > max_height:
        image = image.crop((0, 0, image.width, max_height))
    bordered = Image.new("RGB", (image.width + 2, image.height + 2), (224, 229, 236))
    bordered.paste(image, (1, 1))
    return bordered


def _load_remote_avatar(url: str, size: int) -> Image.Image | None:
    raw = str(url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return None
    try:
        request = Request(
            raw,
            headers={
                "User-Agent": "Mozilla/5.0 NightingaleOpsBot-BiliCard/0.1",
                "Referer": f"{parsed.scheme}://{parsed.netloc}/",
            },
        )
        with urlopen(request, timeout=8) as response:
            data = response.read(1_200_000)
        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGBA")
    except (OSError, URLError, ValueError):
        return None

    side = min(image.width, image.height)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    return image.resize((size, size), RESAMPLE_LANCZOS)


def _wrap_multiline(text: str, font, max_width: int, *, max_lines: int) -> list[str]:
    probe = ImageDraw.Draw(Image.new("RGB", (16, 16)))
    result: list[str] = []
    for paragraph in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = paragraph.strip()
        if not cleaned:
            if result and result[-1] != "":
                result.append("")
            continue
        current = ""
        for char in cleaned:
            candidate = current + char
            if current and _text_width(probe, candidate, font) > max_width:
                result.append(current)
                current = char
                if len(result) >= max_lines:
                    return _truncate_lines(result, max_lines)
                continue
            current = candidate
        if current:
            result.append(current)
        if len(result) >= max_lines:
            return _truncate_lines(result, max_lines)
    return result[:max_lines]


def _normalize_display_text(text: str) -> str:
    value = str(text or "")
    for src, target in DISPLAY_REPLACEMENTS.items():
        value = value.replace(src, target)
    value = value.replace("\ufe0f", "").replace("\u200d", "")
    cleaned_chars: list[str] = []
    for char in value:
        codepoint = ord(char)
        if any(start <= codepoint <= end for start, end in UNSUPPORTED_RANGES):
            continue
        cleaned_chars.append(char)
    normalized = "".join(cleaned_chars)
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _truncate_lines(lines: list[str], max_lines: int) -> list[str]:
    trimmed = lines[:max_lines]
    if trimmed:
        trimmed[-1] = trimmed[-1].rstrip(". ") + "..."
    return trimmed


def _strip_leading_title(summary: str, title: str) -> str:
    value = str(summary or "").strip()
    heading = str(title or "").strip()
    if heading and value.startswith(heading):
        value = value[len(heading) :].lstrip()
    return value


def _first_non_empty_line(text: str) -> str:
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""


def _font(size: int, *, bold: bool = False):
    candidates = list(FONT_CANDIDATES)
    if bold:
        candidates = [
            "/AstrBot/data/plugins/astrbot_plugin_share_link_resolver/.local/fonts/SourceHanSansCN-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
        ] + candidates
    for candidate in candidates:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            if path.suffix.lower() == ".ttc":
                return ImageFont.truetype(str(path), size, index=0)
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _line_height(font) -> int:
    bbox = font.getbbox("Ag")
    return max(1, bbox[3] - bbox[1])


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        return float(max(0, bbox[2] - bbox[0]))


def _draw_text(draw: ImageDraw.ImageDraw, xy, text: str, fill, font) -> None:
    draw.text(xy, str(text or ""), fill=fill, font=font)


def _rounded_rect(draw: ImageDraw.ImageDraw, box, radius: int, fill, outline) -> None:
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)
    except Exception:
        draw.rectangle(box, fill=fill, outline=outline)


def _circle_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    return mask


def _draw_avatar_fallback(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, line, muted, fallback: str) -> None:
    draw.ellipse((x, y, x + size, y + size), fill=(241, 245, 248), outline=line, width=1)
    letter = str(fallback or "B").strip()[:1].upper()
    font = _font(22, bold=True)
    text_w = _text_width(draw, letter, font)
    text_h = _line_height(font)
    draw.text((x + (size - text_w) / 2, y + (size - text_h) / 2 - 1), letter, fill=muted, font=font)
