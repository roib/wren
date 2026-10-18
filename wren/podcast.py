"""Podcast RSS feeds of the library, so native podcast apps can play Wren's episodes."""

import re
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

from .config import MEDIA_DIR

RESERVED = {"singles", "show"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "show"


def feed_slugs(shows: list[dict]) -> dict[int, str]:
    """A readable, unique feed name per show id ("andrew-huberman"). Clashes,
    and names Wren uses itself, get the id appended ("show-7")."""
    out, taken = {}, set(RESERVED)
    for s in sorted(shows, key=lambda s: s["id"]):
        name = slug(s["name"])
        if name in taken:
            name = f"{name}-{s['id']}"
        taken.add(name)
        out[s["id"]] = name
    return out


MIME = {".m4a": "audio/mp4", ".mp4": "video/mp4", ".mp3": "audio/mpeg"}


def _pub_date(ep: dict) -> datetime:
    # upload_date is YYYYMMDD from YouTube; fall back to when Wren downloaded it.
    if ep.get("upload_date"):
        try:
            return datetime.strptime(ep["upload_date"], "%Y%m%d").replace(hour=12, tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(ep["added_at"], timezone.utc)


def _duration(seconds: int | None) -> str | None:
    if not seconds:
        return None
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _item(ep: dict, base: str) -> str:
    path = MEDIA_DIR / ep["file"]
    size = path.stat().st_size if path.exists() else 0
    mime = MIME.get(path.suffix, "video/mp4" if ep["mode"] == "video" else "audio/mp4")
    parts = [
        f"<title>{escape(ep['title'])}</title>",
        f'<guid isPermaLink="false">wren-{escape(ep["video_id"])}-{ep["mode"]}</guid>',
        f"<pubDate>{format_datetime(_pub_date(ep))}</pubDate>",
        f'<enclosure url="{escape(base + "/media/" + ep["file"])}" length="{size}" type="{mime}"/>',
        f"<link>{escape(ep['source_url'])}</link>",
    ]
    if ep.get("description"):
        parts.append(f"<description>{escape(ep['description'])}</description>")
    if d := _duration(ep.get("duration")):
        parts.append(f"<itunes:duration>{d}</itunes:duration>")
    if ep.get("thumb"):
        parts.append(f'<itunes:image href="{escape(base + "/media/" + ep["thumb"])}"/>')
    if ep.get("channel"):
        parts.append(f"<itunes:author>{escape(ep['channel'])}</itunes:author>")
    return "<item>" + "".join(parts) + "</item>"


def render(title: str, description: str, episodes: list[dict], base: str, self_url: str) -> str:
    """An RSS 2.0 feed (with iTunes tags) of the given episodes, newest first."""
    episodes = sorted(episodes, key=_pub_date, reverse=True)
    art = next((ep["thumb"] for ep in episodes if ep.get("thumb")), None)
    author = next((ep["channel"] for ep in episodes if ep.get("channel")), None)
    head = [
        f"<title>{escape(title)}</title>",
        f"<link>{escape(base)}/</link>",
        f"<description>{escape(description)}</description>",
        "<language>en</language>",
        f'<atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml"/>',
        "<itunes:explicit>false</itunes:explicit>",
        "<itunes:type>episodic</itunes:type>",
        # Keep private feeds out of podcast directories.
        "<itunes:block>Yes</itunes:block>",
    ]
    if author:
        head.append(f"<itunes:author>{escape(author)}</itunes:author>")
    if art:
        url = escape(base + "/media/" + art)
        head.append(f'<itunes:image href="{url}"/>')
        head.append(f"<image><url>{url}</url><title>{escape(title)}</title><link>{escape(base)}/</link></image>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel>" + "".join(head) + "".join(_item(ep, base) for ep in episodes) + "</channel></rss>\n"
    )
