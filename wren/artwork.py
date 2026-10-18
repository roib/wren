"""Square, Wren-badged show artwork for the podcast feeds."""

import io
import logging
import re
import urllib.request

import yt_dlp
from PIL import Image, ImageFilter

from pathlib import Path

from .config import MEDIA_DIR
from .ytdl import base_opts

log = logging.getLogger("wren")

SIZE = 1400  # Apple Podcasts wants square art, 1400–3000 px
ART_DIR = MEDIA_DIR / "shows"
# web/favicon.svg rendered to PNG (rsvg-convert -w 512 -h 512), with rounded corners.
BADGE = Path(__file__).with_name("wren-badge.png")


def path_for(show_id: int):
    return ART_DIR / f"{show_id}.jpg"


def singles_path():
    return ART_DIR / "singles.jpg"


def feed_ref(path) -> str | None:
    """The artwork's path relative to MEDIA_DIR for a feed, versioned by its
    modification time so podcast apps notice when it changes."""
    if not path.exists():
        return None
    return f"{path.relative_to(MEDIA_DIR)}?v={int(path.stat().st_mtime)}"


def _avatar_url(info: dict) -> str | None:
    thumbs = info.get("thumbnails") or []
    url = next((t["url"] for t in thumbs if t.get("id") == "avatar_uncropped"), None)
    if not url:  # otherwise the largest square one
        squares = [t for t in thumbs if t.get("width") and t.get("width") == t.get("height")]
        url = max(squares, key=lambda t: t["width"])["url"] if squares else None
    # Google's image server resizes on request: ask for full size.
    return re.sub(r"=s\d+", f"=s{SIZE}", url) if url else None


def channel_avatar(source_url: str) -> str | None:
    """URL of the YouTube channel's profile picture (for a playlist, its owner's)."""
    opts = {**base_opts(), "quiet": True, "no_warnings": True, "extract_flat": True, "playlist_items": "0"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(source_url, download=False, process=False)
        url = _avatar_url(info)
        if not url and info.get("channel_url") and info["channel_url"] != source_url:
            url = _avatar_url(ydl.extract_info(info["channel_url"], download=False, process=False))
    return url


def _fetch(url: str) -> Image.Image:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return Image.open(io.BytesIO(resp.read())).convert("RGB")


def _square(img: Image.Image) -> Image.Image:
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    return img.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), Image.LANCZOS)


def _badge(art: Image.Image) -> Image.Image:
    """The Wren icon in the bottom-right corner, so Wren's copy of a show is easy
    to tell apart from the official one."""
    icon = Image.open(BADGE).convert("RGBA")
    side = SIZE * 22 // 100
    margin = SIZE * 4 // 100
    icon = icon.resize((side, side), Image.LANCZOS)
    # A soft shadow for contrast on light artwork.
    mask = icon.getchannel("A")
    x = y = SIZE - side - margin
    shadow = Image.new("L", art.size, 0)
    shadow.paste(mask, (x, y + side // 40))
    shadow = shadow.filter(ImageFilter.GaussianBlur(side // 12))
    out = art.convert("RGBA")
    out.paste(Image.new("RGBA", art.size, (0, 0, 0, 150)), (0, 0), shadow)
    out.paste(icon, (x, y), mask)
    return out.convert("RGB")


def _save(img: Image.Image, path) -> None:
    ART_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    img.save(tmp, "JPEG", quality=88)
    tmp.replace(path)


def make_show_art(show: dict, fallback_thumb: str | None = None) -> bool:
    """Build the show's badged artwork from its channel picture, or failing that
    from an episode thumbnail (relative to MEDIA_DIR). Returns True if saved."""
    img = None
    try:
        url = channel_avatar(show["source_url"])
        if url:
            img = _fetch(url)
    except Exception as e:  # network, YouTube changes, rate limits: fall back
        log.info("%s: no channel picture (%s)", show["name"], str(e).splitlines()[0][:120])
    if img is None and fallback_thumb and (MEDIA_DIR / fallback_thumb).exists():
        img = Image.open(MEDIA_DIR / fallback_thumb).convert("RGB")
    if img is None:
        return False
    _save(_badge(_square(img)), path_for(show["id"]))
    return True


def ensure_singles_art() -> None:
    """Artwork for the single-episodes feed: the Wren icon on its own."""
    if singles_path().exists():
        return
    side = SIZE * 80 // 100
    icon = Image.open(BADGE).convert("RGBA").resize((side, side), Image.LANCZOS)
    bg = Image.new("RGB", (SIZE, SIZE), (17, 17, 19))  # the icon's own background colour
    bg.paste(icon, ((SIZE - side) // 2,) * 2, icon)
    _save(bg, singles_path())
