from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
import mimetypes
from pathlib import Path
import shutil

from astrbot.api import logger
from PIL import Image, ImageChops

from .feed_client import FeedItem


_FONT_CANDIDATES = (
    Path("/AstrBot/data/plugins/astrbot_plugin_share_link_resolver/.local/fonts/SourceHanSansCN-Regular-subset.woff2"),
    Path("/AstrBot/data/plugins/astrbot_plugin_share_link_resolver/.local/fonts/SourceHanSansCN-Regular.ttc"),
    Path("/AstrBot/data/plugins/astrbot_plugin_weibo_feed/.local/fonts/SourceHanSansCN-Regular.ttc"),
)


def _data_uri(path: str) -> str:
    if not path:
        return ""
    file_path = Path(path)
    if not file_path.is_file():
        return ""
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _template() -> str:
    return (Path(__file__).with_name("weibo_card.html")).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _font_data_uri() -> str:
    for path in _FONT_CANDIDATES:
        if path.is_file():
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            mime = "font/woff2" if path.suffix.lower() == ".woff2" else "font/ttf"
            return f"data:{mime};base64,{encoded}"
    return ""


def _trim_canvas(path: Path) -> None:
    with Image.open(path) as source:
        image = source.convert("RGB")
        background = Image.new("RGB", image.size, image.getpixel((0, 0)))
        difference = ImageChops.difference(image, background).convert("L")
        difference = difference.point(lambda value: 255 if value > 8 else 0)
        bbox = difference.getbbox()
        if not bbox:
            return
        left = max(0, bbox[0] - 8)
        top = max(0, bbox[1] - 8)
        right = min(image.width, bbox[2] + 8)
        bottom = min(image.height, bbox[3] + 8)
        if (left, top, right, bottom) != (0, 0, image.width, image.height):
            image.crop((left, top, right, bottom)).save(path)


async def render_weibo_card_html(
    star,
    item: FeedItem,
    output_dir: Path,
    *,
    image_paths: list[str] | None = None,
    author_avatar_path: str = "",
    brand_avatar_path: str = "",
    brand_name: str = "Yoine❤",
    width: int = 860,
    max_height: int = 2800,
) -> str:
    card_width = max(640, int(width))
    local_images = [str(path) for path in (image_paths or []) if path]
    context = {
        "card_width": card_width,
        "card_content_width": max(592, card_width - 48),
        "card_max_height": max(1200, int(max_height)),
        "author_name": str(item.author_name or "微博用户"),
        "author_avatar": _data_uri(author_avatar_path),
        "published_at": str(item.published_at or ""),
        "title": str(item.title or ""),
        "summary": str(item.summary or item.title or ""),
        "images": [uri for path in local_images if (uri := _data_uri(path))],
        "brand_avatar": _data_uri(brand_avatar_path),
        "brand_name": str(brand_name or "Yoine❤"),
        "font_regular": _font_data_uri(),
    }
    options = {
        "full_page": True,
        "type": "png",
        "scale": "device",
        "device_scale_factor_level": "ultra",
    }
    rendered = await star.html_render(
        tmpl=_template(),
        data=context,
        return_url=False,
        options=options,
    )
    if not rendered:
        raise RuntimeError("AstrBot HTML renderer returned no image")

    output_dir.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha256(
        "|".join(
            [
                "html-card-sourcehan-weibo-v1",
                str(item.item_id),
                str(item.title),
                str(item.summary),
                str(item.published_at),
                str(item.link),
                str(author_avatar_path),
                "|".join(local_images),
                str(brand_avatar_path),
            ]
        ).encode("utf-8")
    ).hexdigest()[:24]
    target = output_dir / f"{signature}.png"
    shutil.copyfile(rendered, target)
    _trim_canvas(target)
    logger.debug("Weibo HTML card rendered: %s", target)
    return str(target)
