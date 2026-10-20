"""Shows followed through their own podcast feed (RSS) instead of YouTube.

For audio-only listening: downloads are plain HTTP from the podcast's host, so
there's no YouTube rate limiting, pacing or yt-dlp involved. The feeds' ads
are mostly inserted per download; they're found in the transcript like any
other (SponsorBlock only knows YouTube videos).
"""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import db
from .config import MEDIA_DIR
from .feeds import Feed, FeedItem

USER_AGENT = "Wren/0.1 (self-hosted podcast downloader)"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
LIST_LIMIT = 30  # newest entries looked at per check
EXTENSIONS = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
              "audio/aac": ".m4a", "audio/ogg": ".ogg", "audio/opus": ".opus"}


def is_feed_url(url: str) -> bool:
    """Anything that isn't YouTube is treated as a podcast feed (or an Apple Podcasts link)."""
    host = urllib.parse.urlparse(url.strip()).netloc
    return bool(host) and not host.endswith(("youtube.com", "youtu.be"))


def item_id(guid: str) -> str:
    """A stable id for a feed entry, safe in file names, and never mistaken for
    an 11-character YouTube id."""
    return "pod" + hashlib.sha256(guid.encode()).hexdigest()[:16]


def _open(url: str, timeout: int = 30):
    """Every request to a podcast host or Apple's directory goes through here."""
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


def _get(url: str, timeout: int = 30) -> bytes:
    with _open(url, timeout) as r:
        return r.read()


def duration(value: str | None) -> int | None:
    """itunes:duration: seconds, "MM:SS" or "HH:MM:SS"."""
    if not value or not value.strip():
        return None
    try:
        parts = [float(p) for p in value.strip().split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return int(seconds) or None


def _published(item) -> tuple[str | None, int | None]:
    try:
        when = parsedate_to_datetime(item.findtext("pubDate") or "")
    except (TypeError, ValueError):
        return None, None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y%m%d"), int(when.timestamp())


def parse(xml: bytes) -> dict:
    """A feed's title, author, artwork and entries (newest first, as feeds list them)."""
    channel = ET.fromstring(xml).find("channel")
    if channel is None:
        raise ValueError("not a podcast feed")
    image = channel.find(f"{ITUNES}image")
    show_image = image.get("href") if image is not None else channel.findtext("image/url")
    entries = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            continue  # no audio: a text post, a trailer page...
        guid = (item.findtext("guid") or enclosure.get("url")).strip()
        ep_image = item.find(f"{ITUNES}image")
        day, ts = _published(item)
        entries.append({
            "id": item_id(guid),
            "title": (item.findtext("title") or "").strip(),
            "audio": enclosure.get("url"),
            "type": enclosure.get("type") or "",
            "duration": duration(item.findtext(f"{ITUNES}duration")),
            "published": day,
            "published_at": ts,
            "description": (item.findtext(f"{ITUNES}summary") or item.findtext("description") or "").strip() or None,
            "link": (item.findtext("link") or "").strip() or None,
            "image": ep_image.get("href") if ep_image is not None else show_image,
            "episode_type": (item.findtext(f"{ITUNES}episodeType") or "full").strip(),
        })
    return {"title": (channel.findtext("title") or "").strip(),
            "author": (channel.findtext(f"{ITUNES}author") or "").strip() or None,
            "image": show_image, "entries": entries}


def fetch(feed_url: str) -> Feed:
    """The feed as Wren's show listing (like feeds.fetch for YouTube)."""
    feed = parse(_get(feed_url))
    items = [FeedItem(item_id=e["id"], url=e["audio"], title=e["title"], duration=e["duration"],
                      live_status=None, thumb_url=e["image"], published=e["published"])
             for e in feed["entries"][:LIST_LIMIT] if e["episode_type"] != "trailer"]
    return Feed(title=feed["title"] or feed_url, items=items)


def show_image(feed_url: str) -> str | None:
    return parse(_get(feed_url))["image"]


# ---- finding a feed ---------------------------------------------------------------

APPLE_ID = re.compile(r"podcasts\.apple\.com/.*/id(\d+)")


def feed_url_for(url: str) -> str:
    """A podcast feed's URL from a pasted feed URL or Apple Podcasts link."""
    url = url.strip()
    m = APPLE_ID.search(url)
    if m:
        results = json.loads(_get(f"https://itunes.apple.com/lookup?id={m.group(1)}&entity=podcast"))["results"]
        if not results or not results[0].get("feedUrl"):
            raise ValueError("Apple Podcasts doesn't list a feed for that show.")
        return results[0]["feedUrl"]
    try:
        parse(_get(url))
    except ET.ParseError:
        raise ValueError("That link isn't a podcast feed.") from None
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:  # gone or never there; a 5xx is worth retrying
            raise ValueError("That link isn't a podcast feed.") from None
        raise
    return url


def search(name: str, limit: int = 6) -> list[dict]:
    """Podcasts matching a name, from Apple's public directory: title, author, feed URL, artwork."""
    query = urllib.parse.urlencode({"term": name, "entity": "podcast", "limit": limit})
    results = json.loads(_get(f"https://itunes.apple.com/search?{query}"))["results"]
    return [{"title": r.get("collectionName"), "author": r.get("artistName"), "feed_url": r["feedUrl"],
             "image": r.get("artworkUrl600") or r.get("artworkUrl100")}
            for r in results if r.get("feedUrl")]


# ---- downloading ----------------------------------------------------------------------

def _extension(audio_url: str, content_type: str) -> str:
    ext = Path(urllib.parse.urlparse(audio_url).path).suffix.lower()
    if ext in (".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus"):
        return ".m4a" if ext in (".mp4", ".aac") else ext
    return EXTENSIONS.get(content_type.split(";")[0].strip().lower(), ".mp3")


def download(feed_url: str, entry_id: str, show_id: int, hold_for_sponsors: bool = False) -> dict:
    """Download one entry of a podcast feed and add it to the library."""
    feed = parse(_get(feed_url))
    entry = next((e for e in feed["entries"] if e["id"] == entry_id), None)
    if entry is None:
        raise RuntimeError("the episode is no longer in the feed")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with _open(entry["audio"], timeout=60) as resp:
        path = MEDIA_DIR / f"{entry_id}{_extension(resp.geturl(), resp.headers.get('Content-Type', entry['type']))}"
        tmp = path.with_name(path.name + ".part")
        with open(tmp, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
    tmp.replace(path)
    thumb = _thumbnail(entry["image"], entry_id)
    episode = {
        "video_id": entry_id, "mode": "audio", "title": entry["title"] or entry_id,
        "channel": feed["author"] or feed["title"], "duration": entry["duration"] or _probe_duration(path),
        "upload_date": entry["published"], "published_at": entry["published_at"],
        "description": entry["description"], "source_url": entry["link"] or entry["audio"],
        "file": path.relative_to(MEDIA_DIR).as_posix(), "thumb": thumb, "show_id": show_id,
        "sponsor_state": "pending" if hold_for_sponsors else None,
    }
    db.upsert_episode(episode)
    return episode


def _thumbnail(url: str | None, entry_id: str) -> str | None:
    """The episode's (or show's) artwork as a JPEG next to it, like YouTube thumbnails."""
    if not url:
        return None
    try:
        import io

        from PIL import Image
        img = Image.open(io.BytesIO(_get(url))).convert("RGB")
        img.thumbnail((640, 640))
        path = MEDIA_DIR / f"{entry_id}.jpg"
        img.save(path, "JPEG", quality=85)
        return path.relative_to(MEDIA_DIR).as_posix()
    except Exception:
        return None  # artwork is nice to have


def _probe_duration(path: Path) -> int | None:
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    try:
        return int(float(out.stdout.strip()))
    except ValueError:
        return None
