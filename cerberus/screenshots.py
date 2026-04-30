"""Screenshot persistence for run evidence.

Saves PNGs under data/screenshots/<run_id>/ and serves them via FastAPI for the report viewer.
The markdown report embeds a small data-URL thumbnail (kept under ~80 KB by JPEG re-compression
with Pillow) so the .md file remains self-contained when shared with the dev team.
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

SCREENSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "screenshots"
THUMBNAIL_MAX_W = 800
THUMBNAIL_QUALITY = 78


def save_png(run_id: str, name: str, png_bytes: bytes) -> Path:
    out = SCREENSHOT_DIR / run_id / f"{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png_bytes)
    return out


def thumbnail_data_url(png_bytes: bytes, max_width: int = THUMBNAIL_MAX_W, quality: int = THUMBNAIL_QUALITY) -> str:
    """Return a data:image/jpeg;base64,... URL of a downsized JPEG of this PNG."""
    if not png_bytes:
        return ""
    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            w, h = im.size
            if w > max_width:
                ratio = max_width / w
                im = im.resize((max_width, int(h * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
        return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    except Exception as exc:
        log.warning("thumbnail failed: %s", exc)
        return ""
