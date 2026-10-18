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
import urllib.request
from dataclasses import dataclass

import yt_dlp

from . import ytdl

LIST_LIMIT = 15  # how many of the newest videos to look at per check
USER_AGENT = "Mozilla/5.0 (compatible; wren/0.1)"


@dataclass
class FeedItem:
    item_id: str
    url: str
    title: str
    duration: int | None
    live_status: str | None
    thumb_url: str | None = None


@dataclass
class Feed:
    title: str
    items: list[FeedItem]  # newest first


def _ydl() -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL({
        **ytdl.base_opts(),
        "quiet": True,
        "extract_flat": "in_playlist",
        "playlistend": LIST_LIMIT,
        "skip_download": True,
    })


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


def thumb_url(video_id: str) -> str:
    """YouTube's standard medium-size thumbnail for a video."""
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def fetch(feed_url: str) -> Feed:
    with _ydl() as ydl:
        info = ydl.extract_info(feed_url, download=False)

    items = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        items.append(FeedItem(
            item_id=entry["id"],
            url=f"https://www.youtube.com/watch?v={entry['id']}",
            title=(entry.get("title") or "").strip(),
            duration=int(entry["duration"]) if entry.get("duration") else None,
            live_status=entry.get("live_status"),
            thumb_url=thumb_url(entry["id"]),
        ))

    list_id = urllib.parse.parse_qs(urllib.parse.urlparse(feed_url).query).get("list", [""])[0]
    # "Uploads from X" reads badly as a show name; use the channel name instead.
    title = info.get("channel") if list_id.startswith("UU") else info.get("title")
    return Feed(title=title or info.get("uploader") or list_id, items=items)
