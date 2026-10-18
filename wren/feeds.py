"""List the latest videos of YouTube channels and playlists.

Uses yt-dlp's flat playlist extraction: one page request returns the newest
video ids, titles, durations and live status without opening any video, so
checking a show is cheap and doesn't touch YouTube's video-player bot checks.
(YouTube's RSS feeds would be cheaper still, but currently return 404.)

Channels are followed through their uploads playlist (UU...), which includes
normal uploads and past livestreams in newest-first order.
"""

import re
import urllib.parse
from datetime import datetime, timezone
import urllib.request
from dataclasses import dataclass

import yt_dlp

from . import ytdl

LIST_LIMIT = 15  # how many of the newest videos to look at per check
# Other playlists may be in any order (often oldest first), so look at more of
# them and sort newest first using the channel's uploads.
PLAYLIST_LIMIT = 200
USER_AGENT = "Mozilla/5.0 (compatible; wren/0.1)"


@dataclass
class FeedItem:
    item_id: str
    url: str
    title: str
    duration: int | None
    live_status: str | None
    thumb_url: str | None = None
    published: str | None = None  # YYYYMMDD, approximate ("3 days ago")


@dataclass
class Feed:
    title: str
    items: list[FeedItem]  # newest first


def _ydl(limit: int = LIST_LIMIT) -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL({
        **ytdl.base_opts(),
        "quiet": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
        "skip_download": True,
        # Flat listings carry no dates; this turns YouTube's "3 days ago" into one.
        "extractor_args": {"youtubetab": {"approximate_date": [""]}},
    })


def _list_id(feed_url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(feed_url).query).get("list", [""])[0]


def _newest_first(entries: list[dict], channel_id: str | None) -> list[dict]:
    """Put a playlist's entries newest first. Flat listings carry no dates, so
    compare against the channel's uploads (newest first): if the playlist's
    videos appear there in reverse order, the playlist runs oldest first."""
    if not channel_id or len(entries) < 2:
        return entries
    with _ydl(50) as ydl:
        uploads = ydl.extract_info(_playlist_url("UU" + channel_id[2:]), download=False)
    rank = {e["id"]: n for n, e in enumerate(uploads.get("entries") or []) if e and e.get("id")}
    ranks = [rank[e["id"]] for e in entries if e.get("id") in rank]
    if len(ranks) < 2:
        # None of the playlist is among the recent uploads: the end that's
        # newest can't be told, unless the playlist's last video is the newest
        # thing we know about. Keep the playlist's order.
        return entries
    rising = sum(b > a for a, b in zip(ranks, ranks[1:]))
    return entries if rising >= len(ranks) - 1 - rising else list(reversed(entries))


def _playlist_url(list_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={list_id}"


def _channel_id_from_page(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as res:
        html = res.read().decode("utf-8", "replace")
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html)
    if m:
        return m.group(1)
    m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', html)
    return m.group(1) if m else None


def _channel_id_from_ytdlp(url: str) -> str | None:
    with _ydl() as ydl:
        info = ydl.extract_info(url, download=False)
    cid = info.get("channel_id") or info.get("id")
    return cid if cid and cid.startswith("UC") else None


def feed_url_for(url: str) -> str:
    """Turn a channel, @handle or playlist URL into the playlist URL we poll."""
    parsed = urllib.parse.urlparse(url.strip())
    if not parsed.netloc.endswith(("youtube.com", "youtu.be")):
        raise ValueError("only YouTube channel and playlist URLs are supported for now")

    query = urllib.parse.parse_qs(parsed.query)
    if "list" in query:
        return _playlist_url(query["list"][0])

    m = re.match(r"/channel/(UC[\w-]{22})", parsed.path)
    channel_id = m.group(1) if m else None
    if not channel_id:
        # @handle, /c/name, /user/name: resolve to the channel id.
        base = re.match(r"/(@[^/]+|c/[^/]+|user/[^/]+)", parsed.path)
        if not base:
            raise ValueError(f"not a channel or playlist URL: {url}")
        channel_url = f"https://www.youtube.com/{base.group(1)}"
        try:
            channel_id = _channel_id_from_page(channel_url)
        except Exception:
            channel_id = None
        channel_id = channel_id or _channel_id_from_ytdlp(channel_url)
    if not channel_id:
        raise ValueError(f"couldn't find a channel at {url}")
    # A channel's uploads playlist is its id with UC replaced by UU.
    return _playlist_url("UU" + channel_id[2:])


def _day(timestamp: int | None) -> str | None:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%d") if timestamp else None


def thumb_url(video_id: str) -> str:
    """YouTube's standard medium-size thumbnail for a video."""
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def fetch(feed_url: str) -> Feed:
    uploads = _list_id(feed_url).startswith("UU")
    with _ydl(LIST_LIMIT if uploads else PLAYLIST_LIMIT) as ydl:
        info = ydl.extract_info(feed_url, download=False)

    # Private and deleted videos stay in playlists as untitled placeholders that
    # can never be downloaded; leave them out (one made public later shows up as new).
    entries = [e for e in info.get("entries") or [] if e and e.get("id") and e.get("title")]
    if not uploads:
        try:
            entries = _newest_first(entries, info.get("channel_id"))
        except Exception:
            pass  # keep the playlist's own order

    items = []
    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        items.append(FeedItem(
            item_id=entry["id"],
            url=f"https://www.youtube.com/watch?v={entry['id']}",
            title=(entry.get("title") or "").strip(),
            duration=int(entry["duration"]) if entry.get("duration") else None,
            live_status=entry.get("live_status"),
            thumb_url=thumb_url(entry["id"]),
            published=_day(entry.get("timestamp")),
        ))

    channel = info.get("channel") or info.get("uploader")
    if uploads:
        # "Uploads from X" reads badly as a show name; use the channel name instead.
        title = channel
    else:
        # A playlist called "Full Episodes" needs its channel's name to make sense.
        # ("Huberman Lab Essentials" already says whose it is.)
        title = info.get("title")
        words = [w for w in re.findall(r"\w+", (channel or "").lower()) if len(w) > 3]
        if title and channel and not any(w in title.lower() for w in words):
            title = f"{channel}: {title}"
    return Feed(title=title or channel or _list_id(feed_url), items=items)
