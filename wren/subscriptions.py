"""Subscriptions: follow shows and download their new episodes.

subscribe() records a show and queues its newest few episodes (the backfill);
sync() checks every show's feed, queues anything new, downloads the queue,
and trims each show to its keep_last limit.
"""

import logging
import re
import time

from . import db, downloader, feeds
from .config import DOWNLOAD_PAUSE_SECONDS, MAX_ATTEMPTS, MEDIA_DIR

log = logging.getLogger("wren")


def _now() -> int:
    return int(time.time())


# ---- shows ------------------------------------------------------------------

def list_shows() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT s.*,
                   (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id) AS episodes,
                   (SELECT COUNT(*) FROM feed_items f WHERE f.show_id = s.id AND f.status = 'queued') AS queued,
                   (SELECT COUNT(*) FROM feed_items f WHERE f.show_id = s.id AND f.status = 'failed') AS failed
            FROM shows s ORDER BY s.name COLLATE NOCASE
        """).fetchall()
    return [dict(r) for r in rows]


def get_show(show_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone()
    return dict(row) if row else None


def subscribe(url: str, mode: str = "audio", keep_last: int = 10, backfill: int = 3,
              min_duration: int = 300, title_filter: str | None = None) -> dict:
    """Add a show. Its newest `backfill` episodes are queued; older ones are ignored."""
    if mode not in ("audio", "video"):
        raise ValueError("mode must be 'audio' or 'video'")
    if title_filter:
        re.compile(title_filter)  # fail now rather than on every sync

    feed_url = feeds.feed_url_for(url)
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM shows WHERE feed_url = ?", (feed_url,)).fetchone():
            raise ValueError("already subscribed to this show")

    feed = feeds.fetch(feed_url)
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO shows (name, kind, source_url, feed_url, mode, keep_last,
                                  min_duration, title_filter, last_checked, created_at)
               VALUES (?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (feed.title or url, url, feed_url, mode, keep_last, min_duration,
             title_filter, _now(), _now()),
        )
        show_id = cur.lastrowid
        show = dict(conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone())

        # Decide newest-first (backfill counts from the newest), insert oldest-first
        # so row order matches upload order.
        decisions, queued = [], 0
        for item in feed.items:
            if _is_pending(item):
                continue  # live or upcoming: pick it up on a later sync
            reason = _feed_skip_reason(show, item)
            if not reason and queued >= backfill:
                reason = "older than backfill"
            decisions.append((item, reason))
            queued += not reason
        for item, reason in reversed(decisions):
            _insert_item(conn, show_id, item, reason)
    log.info("Subscribed to %s (%d queued)", show["name"], queued)
    return show


def unsubscribe(show_id: int) -> None:
    """Remove a show. Its downloaded episodes stay in the library as manual adds."""
    with db.connect() as conn:
        conn.execute("UPDATE episodes SET show_id = NULL WHERE show_id = ?", (show_id,))
        conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))  # feed_items cascade


# ---- feed items ---------------------------------------------------------------

def _is_pending(item: feeds.FeedItem) -> bool:
    """Live now or not aired yet. Not recorded, so a later sync sees it as new."""
    return item.live_status in ("is_live", "is_upcoming")


def _feed_skip_reason(show: dict, item: feeds.FeedItem) -> str | None:
    if show["min_duration"] and item.duration is not None and item.duration < show["min_duration"]:
        return f"too short ({item.duration}s)"
    if show["title_filter"] and not re.search(show["title_filter"], item.title, re.I):
        return "title filter"
    return None


def _insert_item(conn, show_id: int, item: feeds.FeedItem, skip_reason: str | None) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO feed_items
               (show_id, item_id, url, title, published, status, error, first_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (show_id, item.item_id, item.url, item.title, None,
         "skipped" if skip_reason else "queued", skip_reason, _now()),
    )


def _check_feed(show: dict) -> int:
    """Fetch a show's feed and queue items not seen before. Returns how many were queued."""
    try:
        feed = feeds.fetch(show["feed_url"])
    except Exception as e:
        with db.connect() as conn:
            conn.execute("UPDATE shows SET last_checked = ?, last_error = ? WHERE id = ?",
                         (_now(), f"feed: {e}", show["id"]))
        log.warning("%s: couldn't fetch feed: %s", show["name"], e)
        return 0

    with db.connect() as conn:
        seen = {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM feed_items WHERE show_id = ?", (show["id"],))}
        new = [i for i in feed.items if i.item_id not in seen and not _is_pending(i)]
        queued = 0
        for item in reversed(new):  # oldest first, so row order matches upload order
            reason = _feed_skip_reason(show, item)
            _insert_item(conn, show["id"], item, reason)
            queued += not reason
        conn.execute("UPDATE shows SET last_checked = ?, last_error = NULL WHERE id = ?",
                     (_now(), show["id"]))
    if queued:
        log.info("%s: %d new episode(s)", show["name"], queued)
    return queued


def _set_item(show_id: int, item_id: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE feed_items SET {cols} WHERE show_id = ? AND item_id = ?",
                     (*fields.values(), show_id, item_id))


def _download_queue(show: dict) -> int:
    """Download a show's queued items, oldest first. Returns how many succeeded."""
    with db.connect() as conn:
        items = [dict(r) for r in conn.execute(
            """SELECT * FROM feed_items WHERE show_id = ? AND status = 'queued'
               ORDER BY rowid""", (show["id"],))]

    done = 0
    for n, item in enumerate(items):
        if n:
            time.sleep(DOWNLOAD_PAUSE_SECONDS)  # go easy on YouTube
        if db.has_episode(item["item_id"], show["mode"]):
            # Already in the library (e.g. added by hand): just claim it.
            with db.connect() as conn:
                conn.execute("UPDATE episodes SET show_id = ? WHERE video_id = ? AND mode = ?",
                             (show["id"], item["item_id"], show["mode"]))
            _set_item(show["id"], item["item_id"], status="downloaded", error=None)
            continue
        log.info("%s: downloading %s", show["name"], item["title"])
        try:
            downloader.download(item["url"], show["mode"], show_id=show["id"],
                                min_duration=show["min_duration"])
        except downloader.Skipped as e:
            if str(e) == "upcoming":
                # Premiere hasn't aired yet; leave it queued and try again next sync.
                _set_item(show["id"], item["item_id"], error="upcoming")
            else:
                _set_item(show["id"], item["item_id"], status="skipped", error=str(e))
            log.info("%s: skipped %s (%s)", show["name"], item["title"], e)
        except Exception as e:
            attempts = item["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "queued"
            _set_item(show["id"], item["item_id"], status=status, attempts=attempts,
                      error=str(e)[:500])
            log.warning("%s: download failed (%d/%d): %s", show["name"], attempts,
                        MAX_ATTEMPTS, e)
        else:
            _set_item(show["id"], item["item_id"], status="downloaded", error=None)
            done += 1
    return done


# ---- retention ------------------------------------------------------------------

def _apply_retention(show: dict) -> int:
    """Delete the show's oldest episodes beyond keep_last. Returns how many were removed."""
    if show["keep_last"] <= 0:
        return 0
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT video_id, mode, file, thumb FROM episodes WHERE show_id = ?
               ORDER BY upload_date DESC, added_at DESC""", (show["id"],)).fetchall()
        old = rows[show["keep_last"]:]
        for ep in old:
            conn.execute("DELETE FROM episodes WHERE video_id = ? AND mode = ?",
                         (ep["video_id"], ep["mode"]))
            (MEDIA_DIR / ep["file"]).unlink(missing_ok=True)
            # The thumbnail may be shared with the other format of the same video.
            if ep["thumb"] and not conn.execute(
                    "SELECT 1 FROM episodes WHERE thumb = ?", (ep["thumb"],)).fetchone():
                (MEDIA_DIR / ep["thumb"]).unlink(missing_ok=True)
    if old:
        log.info("%s: removed %d old episode(s)", show["name"], len(old))
    return len(old)


# ---- sync -------------------------------------------------------------------------

def sync(show_id: int | None = None) -> dict:
    """Check feeds, download new episodes and apply retention for one or all shows."""
    with db.connect() as conn:
        if show_id is None:
            shows = [dict(r) for r in conn.execute("SELECT * FROM shows WHERE enabled = 1")]
        else:
            shows = [dict(r) for r in conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,))]

    totals = {"shows": len(shows), "new": 0, "downloaded": 0, "removed": 0}
    for show in shows:
        totals["new"] += _check_feed(show)
        totals["downloaded"] += _download_queue(show)
        totals["removed"] += _apply_retention(show)
    return totals
