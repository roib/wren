"""Read models for the management app: everything about the shows, their
episodes and the downloader's state, gathered for the API."""

import re
import shutil
import urllib.parse

import yt_dlp

from . import artwork, db, feeds, podcast, subscriptions, ytdl
from .config import MEDIA_DIR


def _size(rel: str | None) -> int:
    if not rel:
        return 0
    try:
        return (MEDIA_DIR / rel).stat().st_size
    except OSError:
        return 0


def _media(rel: str | None) -> str | None:
    return f"/media/{rel}" if rel else None


def _art_url(path) -> str | None:
    ref = artwork.feed_ref(path)
    return f"/media/{ref}" if ref else None


def _item_state(item: dict) -> str:
    """What's happening to a feed item that isn't in the library."""
    if item["status"] in ("failed", "skipped"):
        return item["status"]
    if subscriptions.current_item == (item["show_id"], item["item_id"]):
        return "downloading"
    if item["error"] == "upcoming":
        return "premiere"
    if item["error"]:
        return "retrying"
    return "queued"


def _show_summaries() -> list[dict]:
    shows = subscriptions.list_shows()
    slugs = podcast.feed_slugs(shows)
    with db.connect() as conn:
        eps = [dict(r) for r in conn.execute(
            "SELECT show_id, mode, file, thumb, subtitles, upload_date, added_at, title FROM episodes")]
        items = [dict(r) for r in conn.execute(
            "SELECT show_id, item_id, title, status, error FROM feed_items WHERE status = 'queued'")]
    for s in shows:
        mine = [e for e in eps if e["show_id"] == s["id"]]
        newest = max(mine, key=lambda e: (e["upload_date"] or "", e["added_at"]), default=None)
        queued = [i for i in items if i["show_id"] == s["id"]]
        states = [_item_state(i) for i in queued]
        s.update(
            feed_path=f"/feeds/{slugs[s['id']]}.xml",
            video_feed_path=f"/feeds/{slugs[s['id']]}-video.xml" if s["mode"] == "audio" else None,
            art_url=_art_url(artwork.path_for(s["id"])),
            bytes=sum(_size(e["file"]) + _size(e["subtitles"]) for e in mine),
            newest_date=newest["upload_date"] if newest else None,
            newest_title=newest["title"] if newest else None,
            downloading=next((i["title"] for i, st in zip(queued, states) if st == "downloading"), None),
            retrying=states.count("retrying"),
            premieres=states.count("premiere"),
        )
    return shows


def overview() -> dict:
    shows = _show_summaries()
    show_ids = {s["id"] for s in shows}
    with db.connect() as conn:
        eps = [dict(r) for r in conn.execute("SELECT show_id, file, subtitles FROM episodes")]
    singles = [e for e in eps if e["show_id"] not in show_ids]
    disk = shutil.disk_usage(MEDIA_DIR)
    artwork.ensure_singles_art()
    return {
        "shows": shows,
        "singles": {
            "episodes": len(singles),
            "bytes": sum(_size(e["file"]) + _size(e["subtitles"]) for e in singles),
            "art_url": _art_url(artwork.singles_path()),
            "feed_path": "/feeds/singles.xml",
        },
        "totals": {
            "episodes": len(eps),
            "bytes": sum(_size(e["file"]) + _size(e["subtitles"]) for e in eps),
        },
        "disk": {"free": disk.free, "total": disk.total},
    }


def _entries_from_episodes(rows: list[dict]) -> list[dict]:
    """Library rows grouped by video, one entry per episode with its copies."""
    by_id: dict[str, dict] = {}
    for r in rows:
        e = by_id.setdefault(r["video_id"], {
            "video_id": r["video_id"],
            "title": r["title"],
            "upload_date": r["upload_date"],
            "added_at": r["added_at"],
            "duration": r["duration"],
            "thumb_url": _media(r["thumb"]),
            "source_url": r["source_url"],
            "state": "downloaded",
            "copies": [],
        })
        e["copies"].append({
            "mode": r["mode"],
            "media_url": _media(r["file"]),
            "bytes": _size(r["file"]),
            "subs_url": _media(r["subtitles"]),
        })
        e["thumb_url"] = e["thumb_url"] or _media(r["thumb"])
    for e in by_id.values():
        e["copies"].sort(key=lambda c: c["mode"])  # audio before video
    return sorted(by_id.values(), key=lambda e: (e["upload_date"] or "", e["added_at"]), reverse=True)


ITEM_LIMIT = 40  # skipped/failed history shown per show


def show_detail(show_id: int) -> dict | None:
    show = next((s for s in _show_summaries() if s["id"] == show_id), None)
    if not show:
        return None
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM episodes WHERE show_id = ?", (show_id,))]
        items = [dict(r) for r in conn.execute(
            """SELECT * FROM feed_items WHERE show_id = ? AND status != 'downloaded'
               ORDER BY rowid DESC LIMIT ?""", (show_id, ITEM_LIMIT))]
    entries = _entries_from_episodes(rows)
    have = {e["video_id"] for e in entries}
    for e in entries:
        modes = {c["mode"] for c in e["copies"]}
        if show["mode"] == "audio" and show["video_copy"] and "video" not in modes:
            e["copy_pending"] = True
    pending = []
    for i in items:
        if i["item_id"] in have:
            continue
        pending.append({
            "video_id": i["item_id"],
            "title": i["title"],
            "duration": i["duration"],
            "thumb_url": i["thumb_url"] or feeds.thumb_url(i["item_id"]),
            "source_url": i["url"],
            "state": _item_state(i),
            "error": i["error"],
            "attempts": i["attempts"],
            "next_try": i["next_try"],
            "first_seen": i["first_seen"],
        })
    return {"show": show, "entries": pending + entries}


def singles_detail() -> dict:
    show_ids = {s["id"] for s in subscriptions.list_shows()}
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM episodes")]
    rows = [r for r in rows if r["show_id"] not in show_ids]
    from . import worker  # jobs live with the worker
    jobs = [j for j in worker.list_jobs(20) if j["status"] in ("queued", "running", "error")]
    pending = [{
        "job_id": j["id"],
        "title": j["title"] or j["url"],
        "source_url": j["url"],
        "mode": j["mode"],
        "state": {"running": "downloading", "error": "failed"}.get(j["status"], "queued"),
        "error": j["error"],
        "first_seen": j["created_at"],
    } for j in jobs]
    return {"entries": pending + _entries_from_episodes(rows)}


# ---- resolving a pasted link --------------------------------------------------------

VIDEO_PATH = re.compile(r"^/(watch|shorts/|live/|embed/)")


def _is_video(url: str) -> bool:
    p = urllib.parse.urlparse(url)
    if p.netloc.endswith("youtu.be"):
        return True
    q = urllib.parse.parse_qs(p.query)
    return bool(VIDEO_PATH.match(p.path)) and ("v" in q or not p.path.startswith("/watch"))


def _subscribed(feed_url: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT id, name, mode FROM shows WHERE feed_url = ?", (feed_url,)).fetchone()
    return dict(row) if row else None


def resolve(url: str) -> dict:
    """Work out what a pasted YouTube link is: a show (channel or playlist), with a
    preview of its latest episodes, or a single video and the channel it's from."""
    url = url.strip()
    p = urllib.parse.urlparse(url)
    if not p.netloc.endswith(("youtube.com", "youtu.be")):
        raise ValueError("Paste a YouTube channel, playlist or video link.")
    # A video inside a playlist link: treat as the video (its playlist is rarely the show).
    if _is_video(url):
        opts = {**ytdl.base_opts(), "quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
        channel_url = info.get("channel_url") or (
            f"https://www.youtube.com/channel/{info['channel_id']}" if info.get("channel_id") else None)
        feed_url = feeds.feed_url_for(channel_url) if channel_url else None
        return {
            "kind": "video",
            "url": info.get("webpage_url") or url,
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "channel_url": channel_url,
            "duration": info.get("duration"),
            "thumb_url": feeds.thumb_url(info["id"]) if info.get("id") else None,
            "subscribed": _subscribed(feed_url) if feed_url else None,
        }
    feed_url = feeds.feed_url_for(url)
    feed = feeds.fetch(feed_url)
    return {
        "kind": "show",
        "url": url,
        "title": feed.title,
        "subscribed": _subscribed(feed_url),
        "latest": [{
            "title": i.title,
            "duration": i.duration,
            "thumb_url": i.thumb_url,
            "live": i.live_status in ("is_live", "is_upcoming"),
        } for i in feed.items[:5]],
    }
