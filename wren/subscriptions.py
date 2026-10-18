"""Subscriptions: follow shows and download their new episodes.

subscribe() records a show and queues its newest few episodes (the backfill);
sync() checks every show's feed, queues anything new, downloads the queue,
and trims each show to its keep_last limit.
"""

import logging
import random
import re
import time

from . import artwork, db, downloader, feeds, push
from .config import DOWNLOAD_PAUSE, MAX_ATTEMPTS, MEDIA_DIR

log = logging.getLogger("wren")


def _now() -> int:
    return int(time.time())


def pause_seconds() -> float:
    """A random gap between downloads, so they don't arrive at YouTube as a burst."""
    return random.uniform(*DOWNLOAD_PAUSE)


def _check_regex(pattern: str | None) -> None:
    if pattern:
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"invalid title filter: {e}") from None


# ---- shows ------------------------------------------------------------------

def list_shows() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT s.*,
                   (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id AND e.mode = s.mode) AS episodes,
                   (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id AND e.mode = 'video'
                      AND s.mode = 'audio') AS video_copies,
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
    _check_regex(title_filter)  # fail now rather than on every sync

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


EDITABLE = {"name", "mode", "keep_last", "min_duration", "title_filter", "enabled", "video_copy"}


def update_show(show_id: int, **fields) -> dict:
    """Change a subscription's settings. A lower keep_last takes effect immediately."""
    unknown = set(fields) - EDITABLE
    if unknown:
        raise ValueError(f"can't change {', '.join(sorted(unknown))}")
    if "mode" in fields and fields["mode"] not in ("audio", "video"):
        raise ValueError("mode must be 'audio' or 'video'")
    _check_regex(fields.get("title_filter"))
    for flag in ("enabled", "video_copy"):
        if flag in fields:
            fields[flag] = int(bool(fields[flag]))
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with db.connect() as conn:
            if not conn.execute(f"UPDATE shows SET {cols} WHERE id = ?",
                                (*fields.values(), show_id)).rowcount:
                raise KeyError(show_id)
    show = get_show(show_id)
    if show is None:
        raise KeyError(show_id)
    if "keep_last" in fields:
        _apply_retention(show)
    return show


def requeue_item(show_id: int, item_id: str) -> bool:
    """Queue a skipped or failed episode again, with fresh attempts."""
    with db.connect() as conn:
        return bool(conn.execute(
            """UPDATE feed_items SET status = 'queued', attempts = 0, error = NULL, next_try = 0
               WHERE show_id = ? AND item_id = ? AND status IN ('skipped', 'failed')""",
            (show_id, item_id)).rowcount)


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
               (show_id, item_id, url, title, published, status, error, first_seen,
                duration, thumb_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (show_id, item.item_id, item.url, item.title, item.published,
         "skipped" if skip_reason else "queued", skip_reason, _now(),
         item.duration, item.thumb_url),
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
        push.alert(f"showerror:{show['id']}", f"{show['name']}: can't check for episodes",
                   push.short_error(e), f"/#/show/{show['id']}")
        return 0
    push.clear(f"showerror:{show['id']}")

    with db.connect() as conn:
        seen = {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM feed_items WHERE show_id = ?", (show["id"],))}
        new = [i for i in feed.items if i.item_id not in seen and not _is_pending(i)]
        # Items recorded before dates were kept get theirs filled in.
        for i in feed.items:
            if i.item_id in seen and i.published:
                conn.execute("UPDATE feed_items SET published = ? WHERE show_id = ? AND item_id = ? "
                             "AND published IS NULL", (i.published, show["id"], i.item_id))
        queued = 0
        for item in reversed(new):  # oldest first, so row order matches upload order
            reason = _feed_skip_reason(show, item)
            _insert_item(conn, show["id"], item, reason)
            queued += not reason
        conn.execute("UPDATE shows SET last_checked = ?, last_error = NULL WHERE id = ?",
                     (_now(), show["id"]))
    if queued:
        log.info("%s: %d new episode(s)", show["name"], queued)
    ensure_art(show)
    return queued


def ensure_art(show: dict) -> None:
    """Make the show's podcast-feed artwork if it doesn't have any yet."""
    if artwork.path_for(show["id"]).exists():
        return
    with db.connect() as conn:
        row = conn.execute("SELECT thumb FROM episodes WHERE show_id = ? AND thumb IS NOT NULL "
                           "ORDER BY added_at DESC LIMIT 1", (show["id"],)).fetchone()
    try:
        if artwork.make_show_art(show, row["thumb"] if row else None):
            log.info("%s: made podcast artwork", show["name"])
    except Exception as e:
        log.warning("%s: couldn't make podcast artwork: %s", show["name"], e)


def _set_item(show_id: int, item_id: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE feed_items SET {cols} WHERE show_id = ? AND item_id = ?",
                     (*fields.values(), show_id, item_id))


RETRY_BASE_SECONDS = 15 * 60
RETRY_MAX_SECONDS = 6 * 60 * 60
UPCOMING_RECHECK_SECONDS = 60 * 60
CLAIM_SECONDS = 60 * 60  # how long a claimed item is hidden from other workers


# The subscription item being downloaded right now, for the app's "Downloading…" row.
current_item: tuple[int, str] | None = None


def _download_item(show: dict, item: dict) -> bool:
    """Try to download one feed item and record the outcome. True if it was downloaded."""
    global current_item
    current_item = (show["id"], item["item_id"])
    try:
        return _try_download(show, item)
    finally:
        current_item = None


def _try_download(show: dict, item: dict) -> bool:
    if db.has_episode(item["item_id"], show["mode"]):
        # Already in the library (e.g. added by hand): just claim it.
        with db.connect() as conn:
            conn.execute("UPDATE episodes SET show_id = ? WHERE video_id = ? AND mode = ?",
                         (show["id"], item["item_id"], show["mode"]))
        _set_item(show["id"], item["item_id"], status="downloaded", error=None, next_try=0)
        return True

    log.info("%s: downloading %s", show["name"], item["title"])
    try:
        downloader.download(item["url"], show["mode"], show_id=show["id"],
                            min_duration=show["min_duration"])
    except downloader.Skipped as e:
        if str(e) == "upcoming":
            # Premiere hasn't aired yet; keep it queued and look again later.
            _set_item(show["id"], item["item_id"], error="upcoming",
                      next_try=_now() + UPCOMING_RECHECK_SECONDS)
        else:
            _set_item(show["id"], item["item_id"], status="skipped", error=str(e), next_try=0)
        log.info("%s: skipped %s (%s)", show["name"], item["title"], e)
        return False
    except downloader.Unavailable as e:
        # Private, members-only or removed: retrying won't help.
        _set_item(show["id"], item["item_id"], status="skipped", error=str(e), next_try=0)
        log.info("%s: skipped %s (%s)", show["name"], item["title"] or item["item_id"], e)
        return False
    except downloader.RateLimited as e:
        # Not this episode's fault: don't use up an attempt, just release it and
        # let the caller pause everything.
        _set_item(show["id"], item["item_id"], error=str(e)[:500], next_try=0)
        log.warning("%s: YouTube is rate-limiting us: %s", show["name"], e)
        raise
    except Exception as e:
        attempts = item["attempts"] + 1
        status = "failed" if attempts >= MAX_ATTEMPTS else "queued"
        backoff = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** (attempts - 1))
        _set_item(show["id"], item["item_id"], status=status, attempts=attempts,
                  error=str(e)[:500], next_try=_now() + backoff)
        log.warning("%s: download failed (%d/%d): %s", show["name"], attempts,
                    MAX_ATTEMPTS, e)
        if status == "failed":
            push.alert(f"failed:{show['id']}:{item['item_id']}", f"{show['name']}: download failed",
                       f"“{item['title'] or item['item_id']}” gave up after {attempts} tries: {push.short_error(e)}",
                       f"/#/show/{show['id']}")
        return False

    _set_item(show["id"], item["item_id"], status="downloaded", error=None, next_try=0)
    _apply_retention(show)
    return True


def _download_queue(show: dict) -> int:
    """Try every queued item of a show once, oldest first (ignores retry backoff).
    Returns how many were downloaded."""
    with db.connect() as conn:
        items = [dict(r) for r in conn.execute(
            """SELECT * FROM feed_items WHERE show_id = ? AND status = 'queued'
               ORDER BY rowid""", (show["id"],))]
    done = 0
    for n, item in enumerate(items):
        if n:
            time.sleep(pause_seconds())  # go easy on YouTube
        try:
            done += _download_item(show, item)
        except downloader.RateLimited:
            log.warning("Stopping: YouTube is rate-limiting this machine; try again later.")
            break
    return done


def download_next(show_ids: list[int] | None = None) -> bool:
    """Download the oldest queued item that's due, across all enabled shows
    (or only the given ones). Returns False when there was nothing to do."""
    now = _now()
    only = ""
    params: list = [now]
    if show_ids is not None:
        only = f" AND s.id IN ({','.join('?' * len(show_ids))})" if show_ids else " AND 0"
        params += list(show_ids)
    with db.connect() as conn:
        row = conn.execute(
            f"""SELECT f.*, s.id AS sid FROM feed_items f JOIN shows s ON s.id = f.show_id
                WHERE f.status = 'queued' AND f.next_try <= ? AND s.enabled = 1{only}
                ORDER BY f.rowid LIMIT 1""", params).fetchone()
        if not row:
            return False
        # Claim it, so a concurrent `wren sync` (or a second worker) leaves it alone.
        claimed = conn.execute(
            """UPDATE feed_items SET next_try = ? WHERE show_id = ? AND item_id = ?
               AND status = 'queued' AND next_try <= ?""",
            (now + CLAIM_SECONDS, row["show_id"], row["item_id"], now)).rowcount
        show = dict(conn.execute("SELECT * FROM shows WHERE id = ?", (row["sid"],)).fetchone())
    if claimed:
        _download_item(show, dict(row))
    return True


# ---- video copies -----------------------------------------------------------------
# Audio shows with video_copy on also get each downloaded episode as video (for
# the show's video podcast feed). Works through what's already downloaded, so
# turning it on fills in existing episodes too.

COPY_RETRY_SECONDS = 60 * 60
_copy_failures: dict[str, tuple[int, int]] = {}  # video_id -> (attempts, next_try); reset on restart


def _missing_copy(show_ids: list[int] | None) -> tuple[dict, dict] | None:
    only, params = "", []
    if show_ids is not None:
        only = f" AND s.id IN ({','.join('?' * len(show_ids))})" if show_ids else " AND 0"
        params = list(show_ids)
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT e.video_id, e.title, e.source_url, s.id AS sid FROM episodes e
                JOIN shows s ON s.id = e.show_id
                WHERE e.mode = 'audio' AND s.mode = 'audio' AND s.video_copy = 1 AND s.enabled = 1{only}
                  AND NOT EXISTS (SELECT 1 FROM episodes v WHERE v.video_id = e.video_id AND v.mode = 'video')
                ORDER BY e.upload_date DESC, e.added_at DESC""", params).fetchall()
        now = _now()
        for r in rows:
            attempts, next_try = _copy_failures.get(r["video_id"], (0, 0))
            if attempts < MAX_ATTEMPTS and next_try <= now:
                show = dict(conn.execute("SELECT * FROM shows WHERE id = ?", (r["sid"],)).fetchone())
                return show, dict(r)
    return None


def download_next_copy(show_ids: list[int] | None = None) -> bool:
    """Download one missing video copy. Returns False when there was nothing to do."""
    found = _missing_copy(show_ids)
    if not found:
        return False
    show, ep = found
    log.info("%s: downloading video copy of %s", show["name"], ep["title"])
    try:
        downloader.download(ep["source_url"], "video", show_id=show["id"])
    except downloader.RateLimited:
        raise
    except downloader.Unavailable as e:
        _copy_failures[ep["video_id"]] = (MAX_ATTEMPTS, 0)  # never going to work
        log.info("%s: no video copy of %s (%s)", show["name"], ep["title"], e)
        return True
    except Exception as e:
        attempts = _copy_failures.get(ep["video_id"], (0, 0))[0] + 1
        _copy_failures[ep["video_id"]] = (attempts, _now() + COPY_RETRY_SECONDS * attempts)
        log.warning("%s: video copy failed (%d/%d): %s", show["name"], attempts, MAX_ATTEMPTS, e)
        return True
    _copy_failures.pop(ep["video_id"], None)
    _apply_retention(show)
    return True


def list_pending() -> list[dict]:
    """Queued episodes of enabled shows, oldest first, with what's happening to each:
    downloading, queued, retrying (failed, waiting to try again) or premiere."""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT f.show_id, s.name AS show, f.item_id, f.title, f.duration, f.thumb_url,
                      f.attempts, f.error, f.next_try
               FROM feed_items f JOIN shows s ON s.id = f.show_id
               WHERE f.status = 'queued' AND s.enabled = 1
               ORDER BY f.rowid""")]
    for r in rows:
        if current_item == (r["show_id"], r["item_id"]):
            r["state"] = "downloading"
        elif r["error"] == "upcoming":
            r["state"] = "premiere"
        elif r["error"]:
            r["state"] = "retrying"
        else:
            r["state"] = "queued"
        # Items queued before thumbnails were stored still get one.
        r["thumb_url"] = r["thumb_url"] or feeds.thumb_url(r["item_id"])
    return rows


# ---- retention ------------------------------------------------------------------

def _remove_episode(conn, ep) -> None:
    """Delete one copy of an episode: its library row and its files."""
    conn.execute("DELETE FROM episodes WHERE video_id = ? AND mode = ?", (ep["video_id"], ep["mode"]))
    (MEDIA_DIR / ep["file"]).unlink(missing_ok=True)
    if ep["subtitles"]:
        (MEDIA_DIR / ep["subtitles"]).unlink(missing_ok=True)
    # The thumbnail may be shared with the other format of the same video.
    if ep["thumb"] and not conn.execute(
            "SELECT 1 FROM episodes WHERE thumb = ?", (ep["thumb"],)).fetchone():
        (MEDIA_DIR / ep["thumb"]).unlink(missing_ok=True)


def delete_episode(video_id: str) -> int:
    """Delete a downloaded episode (all its copies) from the server. Its feed item
    becomes "skipped" (deleted), so it isn't downloaded again but can be brought
    back from the show's skipped list. Returns how many copies were removed."""
    with db.connect() as conn:
        rows = conn.execute("SELECT video_id, mode, file, thumb, subtitles, title FROM episodes WHERE video_id = ?",
                            (video_id,)).fetchall()
        for ep in rows:
            _remove_episode(conn, ep)
        conn.execute("UPDATE feed_items SET status = 'skipped', error = 'deleted', next_try = 0 "
                     "WHERE item_id = ? AND status = 'downloaded'", (video_id,))
    if rows:
        log.info("Deleted %s (%s)", rows[0]["title"], ", ".join(r["mode"] for r in rows))
    return len(rows)


def _apply_retention(show: dict) -> int:
    """Delete the show's oldest episodes beyond keep_last (counted separately for
    audio and video copies). Returns how many were removed."""
    if show["keep_last"] <= 0:
        return 0
    with db.connect() as conn:
        old = []
        for mode in ("audio", "video"):
            rows = conn.execute(
                """SELECT video_id, mode, file, thumb, subtitles FROM episodes WHERE show_id = ? AND mode = ?
                   ORDER BY upload_date DESC, added_at DESC""", (show["id"], mode)).fetchall()
            old += rows[show["keep_last"]:]
        for ep in old:
            _remove_episode(conn, ep)
    if old:
        log.info("%s: removed %d old episode(s)", show["name"], len(old))
    return len(old)


# ---- sync -------------------------------------------------------------------------

def check_feeds(show_ids: list[int] | None = None) -> int:
    """Check enabled shows (or the given ones) for new episodes. Returns how many were queued."""
    with db.connect() as conn:
        if show_ids is None:
            shows = [dict(r) for r in conn.execute("SELECT * FROM shows WHERE enabled = 1")]
        else:
            marks = ",".join("?" * len(show_ids))
            shows = [dict(r) for r in conn.execute(
                f"SELECT * FROM shows WHERE id IN ({marks})", show_ids)]
    return sum(_check_feed(show) for show in shows)


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
