"""Read models for the management app: everything about the shows, their
episodes and the downloader's state, gathered for the API."""

import json
import re
import shutil
import time
import urllib.parse

import yt_dlp

from . import artwork, books, db, feeds, podcast, podcasts, sponsors, subscriptions, ytdl
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
        recent_count = conn.execute("SELECT COUNT(DISTINCT video_id) FROM episodes WHERE added_at >= ?",
                                    (int(time.time()) - 86400,)).fetchone()[0]
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
        # The home screen's "Recently downloaded" and the sidebar's count of the last day.
        "recent": recent(HOME_RECENT, shows),
        "recent_count": recent_count,
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
            "number": r.get("number"),
            "state": "downloaded",
            "copies": [],
            "sponsors": None,
        })
        e["copies"].append({
            "mode": r["mode"],
            "added_at": r["added_at"],
            "media_url": _media(r["file"]),
            "bytes": _size(r["file"]),
            "subs_url": _media(r["subtitles"]),
        })
        e["thumb_url"] = e["thumb_url"] or _media(r["thumb"])
        if r["sponsor_state"] == "pending":
            # Held back from the feeds until its sponsor reads are cut (sponsors.py).
            e["sponsors"] = {"state": "pending", "ready_at": sponsors.ready_at(r)}
        elif r["sponsor_state"] == "done" and (e.get("sponsors") or {}).get("state") != "pending":
            decision = json.loads(r["sponsor_cuts"] or "null") or {}
            e["sponsors"] = {"state": "done", "removed": r["sponsor_removed"] or 0,
                             "count": len(json.loads(r["sponsor_segments"] or "[]")),
                             "cuts": decision.get("cuts", []), "note": decision.get("note")}
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
    if show["kind"] == "book":
        entries.sort(key=lambda e: e["number"] or 0)  # in reading order
        items.reverse()
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
            "upload_date": i["published"],
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
    artwork.ensure_singles_art()
    return {"art_url": _art_url(artwork.singles_path()), "entries": pending + _entries_from_episodes(rows)}


RECENT_LIMIT = 50  # episodes on the Recent page
HOME_RECENT = 3  # and on the home screen


def recent(limit: int = RECENT_LIMIT, shows: list[dict] | None = None) -> list[dict]:
    """The latest downloads across every show and the singles, newest first,
    each with the show it belongs to. Only what's still on the server: episodes
    removed by a show's keep-last limit drop out."""
    names = {s["id"]: s["name"] for s in (shows if shows is not None else subscriptions.list_shows())}
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM episodes ORDER BY added_at DESC")]
    entries = _entries_from_episodes(rows)
    show_of = {r["video_id"]: r["show_id"] for r in rows}
    for e in entries:
        sid = show_of[e["video_id"]]
        e["show_id"] = sid if sid in names else None
        e["show"] = names.get(sid, "Single episodes")
    # Rows came newest first, so each entry's added_at is its latest copy's.
    return sorted(entries, key=lambda e: e["added_at"], reverse=True)[:limit]


ACTIVITY_DAYS = 14


def activity(days: int = ACTIVITY_DAYS) -> dict:
    """Every download of the last `days` days, oldest first, for the Status
    page's chart. From the download log, so deleted episodes still count."""
    names = {s["id"]: s["name"] for s in subscriptions.list_shows()}
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, video_id, mode, show_id, title, bytes, at FROM download_log WHERE at >= ? ORDER BY at",
            (int(time.time()) - days * 86400,))]
    for r in rows:
        r["show"] = names.get(r["show_id"], "Single episodes")
    return {"days": days, "downloads": rows}


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


def _playlist_choice(video_url: str) -> dict | None:
    """The playlist a shared video was opened from (…watch?v=…&list=PL…), so it
    can be followed instead of the whole channel. Mixes and Watch Later aren't shows."""
    list_id = urllib.parse.parse_qs(urllib.parse.urlparse(video_url).query).get("list", [""])[0]
    if not list_id.startswith(("PL", "OL")):
        return None
    url = f"https://www.youtube.com/playlist?list={list_id}"
    try:
        feed = feeds.fetch(url)
    except Exception:
        return None
    return {"url": url, "title": feed.title, "episodes": len(feed.items), "subscribed": _subscribed(url)}


def _words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2 and w not in {"the", "and", "with", "show",
                                                                                          "podcast", "official"}}


def same_show(shows: list[dict], name: str, kind: str) -> list[dict]:
    """Shows followed from the other source whose name looks like this one
    ("Theo Von" / "This Past Weekend w/ Theo Von"): likely the same show."""
    mine = _words(name)
    out = []
    for s in shows:
        theirs = _words(s["name"])
        if s["kind"] != kind and mine and theirs and (mine <= theirs or theirs <= mine):
            out.append({"id": s["id"], "name": s["name"], "kind": s["kind"]})
    return out


def _podcast_show(feed_url: str) -> dict:
    feed = podcasts.fetch(feed_url)
    return {
        "kind": "show", "source": "rss", "url": feed_url, "title": feed.title,
        "subscribed": _subscribed(feed_url),
        "same_show": same_show(subscriptions.list_shows(), feed.title, "rss"),
        "latest": [{"title": i.title, "duration": i.duration, "thumb_url": i.thumb_url, "live": False}
                   for i in feed.items[:5]],
    }


def _book(source: str) -> dict:
    book = books.read(source)
    thumb = books.cover(source)
    return {
        "kind": "book", "url": source, "title": book["title"], "author": book["author"], "parts": book["parts"],
        "duration": round(sum(c.end - c.start for c in book["chapters"])),
        "thumb_url": f"/media/{thumb}" if thumb else None,
        "subscribed": _subscribed(source),
        "chapters": [{"title": c.title, "duration": round(c.end - c.start)} for c in book["chapters"]],
    }


def resolve(url: str) -> dict:
    """Work out what was pasted: a YouTube show (channel or playlist) or podcast
    feed, with a preview of its latest episodes; a single YouTube video and the
    channel it's from; or, for a name, the podcasts it could be."""
    url = url.strip()
    if not url:
        raise ValueError("Paste a link, or type a show's name.")
    if books.is_book(url):
        return _book(books.source_for(url))
    p = urllib.parse.urlparse(url)
    if not p.netloc:
        results = podcasts.search(url)
        for r in results:
            r["subscribed"] = _subscribed(r["feed_url"])
        return {"kind": "search", "query": url, "results": results}
    if podcasts.is_feed_url(url):
        return _podcast_show(podcasts.feed_url_for(url))
    # A video inside a playlist link: treat as the video (its playlist is rarely the show).
    if _is_video(url):
        # A …watch?v=…&list=… link means this video (its playlist is offered separately).
        vid = urllib.parse.parse_qs(p.query).get("v", [None])[0]
        video_url = f"https://www.youtube.com/watch?v={vid}" if vid else url
        opts = {**ytdl.base_opts(), "quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False, process=False)
        channel_url = info.get("channel_url") or (
            f"https://www.youtube.com/channel/{info['channel_id']}" if info.get("channel_id") else None)
        feed_url = feeds.feed_url_for(channel_url) if channel_url else None
        return {
            "playlist": _playlist_choice(url),
            "kind": "video",
            "url": info.get("webpage_url") or video_url,
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
        "source": "youtube",
        "url": url,
        "title": feed.title,
        "subscribed": _subscribed(feed_url),
        "same_show": same_show(subscriptions.list_shows(), feed.title, "youtube"),
        "latest": [{
            "title": i.title,
            "duration": i.duration,
            "thumb_url": i.thumb_url,
            "live": i.live_status in ("is_live", "is_upcoming"),
        } for i in feed.items[:5]],
    }
