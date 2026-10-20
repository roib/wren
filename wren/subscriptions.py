"""Subscriptions: follow shows and download their new episodes.

subscribe() records a show and queues its newest few episodes (the backfill);
sync() checks every show's feed, queues anything new, downloads the queue,
and trims each show to its keep_last limit.
"""

import logging
import random
import re
import time

from . import artwork, books, db, downloader, feeds, podcasts, push, transcribe
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


def source_kind(url: str) -> str:
    """"book" for an audiobook in the books folder, "rss" for a podcast feed or
    Apple Podcasts link, "youtube" otherwise."""
    if books.is_book(url):
        return "book"
    return "rss" if podcasts.is_feed_url(url) else "youtube"


def _feed_url_for(kind: str, url: str) -> str:
    if kind == "book":
        return books.source_for(url)
    return podcasts.feed_url_for(url) if kind == "rss" else feeds.feed_url_for(url)


def _fetch(kind: str, feed_url: str) -> feeds.Feed:
    if kind == "book":
        return books.fetch(feed_url)
    return podcasts.fetch(feed_url) if kind == "rss" else feeds.fetch(feed_url)


def subscribe(url: str, mode: str = "audio", keep_last: int = 10, backfill: int = 3,
              min_duration: int = 300, title_filter: str | None = None,
              skip_sponsors: bool = False, skip_intros: bool = False) -> dict:
    """Add a show, from YouTube or its podcast feed. Its newest `backfill`
    episodes are queued; older ones are ignored. The sponsor settings are saved
    before anything downloads, so the first episodes are cut too."""
    if mode not in ("audio", "video"):
        raise ValueError("mode must be 'audio' or 'video'")
    _check_regex(title_filter)  # fail now rather than on every sync
    kind = source_kind(url)
    if kind == "rss" and mode == "video":
        raise ValueError("podcast feeds are audio only; follow the show on YouTube for video")
    author = None
    if kind == "book":
        # Every chapter, all kept, none too short; and no ads to look for.
        mode, keep_last, min_duration, title_filter, skip_sponsors = "audio", 0, 0, None, False
        url = books.source_for(url)
        author = books.read(url)["author"]
        backfill = 1 << 30

    feed_url = _feed_url_for(kind, url)
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM shows WHERE feed_url = ?", (feed_url,)).fetchone():
            raise ValueError("already subscribed to this show")

    feed = _fetch(kind, feed_url)
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO shows (name, kind, source_url, feed_url, mode, keep_last, min_duration, title_filter,
                                  skip_sponsors, skip_intros, author, last_checked, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (feed.title or url, kind, url, feed_url, mode, keep_last, min_duration, title_filter,
             int(bool(skip_sponsors)), int(bool(skip_sponsors and skip_intros and kind == "youtube")),
             author, _now(), _now()),
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


EDITABLE = {"name", "mode", "keep_last", "min_duration", "title_filter", "enabled", "video_copy", "skip_sponsors",
            "skip_intros"}


def update_show(show_id: int, **fields) -> dict:
    """Change a subscription's settings. A lower keep_last takes effect immediately."""
    unknown = set(fields) - EDITABLE
    if unknown:
        raise ValueError(f"can't change {', '.join(sorted(unknown))}")
    if "mode" in fields and fields["mode"] not in ("audio", "video"):
        raise ValueError("mode must be 'audio' or 'video'")
    current = get_show(show_id)
    if current and current["kind"] == "rss" and (fields.get("mode") == "video" or fields.get("video_copy")):
        raise ValueError("podcast feeds are audio only; switch the show to YouTube for video")
    if current and current["kind"] == "book" and set(fields) - {"name"}:
        raise ValueError("a book keeps all its chapters as audio; only its name can change")
    _check_regex(fields.get("title_filter"))
    for flag in ("enabled", "video_copy", "skip_sponsors", "skip_intros"):
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


def switch_source(show_id: int, url: str) -> dict:
    """Follow a show through another source (its podcast feed, or YouTube) from
    now on. Its settings, episodes and Wren feed stay; everything already in the
    new source counts as seen, so nothing is downloaded twice."""
    show = get_show(show_id)
    if show is None:
        raise KeyError(show_id)
    kind = source_kind(url)
    if "book" in (kind, show["kind"]):
        raise ValueError("a book can't be switched to another source")
    feed_url = _feed_url_for(kind, url)
    with db.connect() as conn:
        other = conn.execute("SELECT name FROM shows WHERE feed_url = ? AND id != ?", (feed_url, show_id)).fetchone()
    if other:
        raise ValueError(f"{other['name']} already follows that")
    feed = _fetch(kind, feed_url)
    with db.connect() as conn:
        conn.execute("""UPDATE shows SET kind = ?, source_url = ?, feed_url = ?, last_checked = ?, last_error = NULL,
                        mode = CASE WHEN ? = 'rss' THEN 'audio' ELSE mode END,
                        video_copy = CASE WHEN ? = 'rss' THEN 0 ELSE video_copy END WHERE id = ?""",
                     (kind, url, feed_url, _now(), kind, kind, show_id))
        # What's still queued from the old source isn't wanted any more.
        conn.execute("UPDATE feed_items SET status = 'skipped', error = 'source changed' WHERE show_id = ? "
                     "AND status = 'queued'", (show_id,))
        for item in reversed(feed.items):
            _insert_item(conn, show_id, item, "from before the switch")
    log.info("%s now follows %s (%s)", show["name"], feed_url, kind)
    return get_show(show_id)


def requeue_item(show_id: int, item_id: str) -> bool:
    """Queue a skipped or failed episode again, with fresh attempts."""
    push.resolve(f"failed:{show_id}:{item_id}")
    with db.connect() as conn:
        return bool(conn.execute(
            """UPDATE feed_items SET status = 'queued', attempts = 0, error = NULL, next_try = 0
               WHERE show_id = ? AND item_id = ? AND status IN ('skipped', 'failed')""",
            (show_id, item_id)).rowcount)


def unsubscribe(show_id: int) -> None:
    """Remove a show. Its downloaded episodes stay in the library as manual adds;
    a book's chapters are deleted with it (its files in the books folder stay)."""
    show = get_show(show_id)
    with db.connect() as conn:
        if show and show["kind"] == "book":
            for ep in conn.execute("SELECT video_id, mode, file, thumb, subtitles FROM episodes WHERE show_id = ?",
                                   (show_id,)).fetchall():
                _remove_episode(conn, ep)
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
        feed = _fetch(show["kind"], show["feed_url"])
    except Exception as e:
        with db.connect() as conn:
            conn.execute("UPDATE shows SET last_checked = ?, last_error = ? WHERE id = ?",
                         (_now(), f"feed: {e}", show["id"]))
        log.warning("%s: couldn't fetch feed: %s", show["name"], e)
        push.alert(f"showerror:{show['id']}", f"{show['name']}: can't check for episodes",
                   push.short_error(e), f"/#/show/{show['id']}")
        return 0
    push.resolve(f"showerror:{show['id']}")

    with db.connect() as conn:
        seen = {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM feed_items WHERE show_id = ?", (show["id"],))}
        new = [i for i in feed.items if i.item_id not in seen and not _is_pending(i)]
        if show["kind"] == "book":
            # How chapters are named can improve, and an epub added later knows
            # the real author (the files' tags often name the publisher);
            # podcast apps pick up both for the same episode.
            author = feed.author or show["author"]
            if author != show["author"]:
                conn.execute("UPDATE shows SET author = ? WHERE id = ?", (author, show["id"]))
            for i in feed.items:
                if i.item_id in seen:
                    conn.execute("UPDATE feed_items SET title = ? WHERE show_id = ? AND item_id = ?",
                                 (i.title, show["id"], i.item_id))
                    conn.execute("UPDATE episodes SET title = ?, channel = ? WHERE video_id = ?",
                                 (i.title, author, i.item_id))
            if feed.items:
                _replace_old_chapters(conn, show, {i.item_id for i in feed.items})
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


def _replace_old_chapters(conn, show: dict, current: set[str]) -> None:
    """A book's chapters were cut differently this time (its epub placed them):
    the old ones, from files still there, go, so nothing is in the book twice.
    (Chapters from files since deleted stay: they're all that's left of them.)"""
    old = conn.execute("SELECT item_id, url FROM feed_items WHERE show_id = ? AND status != 'skipped'",
                       (show["id"],)).fetchall()
    gone = [r["item_id"] for r in old if r["item_id"] not in current
            and (books.BOOKS_DIR / books.item_part(r["url"])).is_file()]
    for item_id in gone:
        for ep in conn.execute("SELECT video_id, mode, file, thumb, subtitles FROM episodes WHERE video_id = ?",
                               (item_id,)).fetchall():
            _remove_episode(conn, ep)
        conn.execute("UPDATE feed_items SET status = 'skipped', error = 'replaced' WHERE show_id = ? AND item_id = ?",
                     (show["id"], item_id))
    if gone:
        log.info("%s: replaced %d chapter(s) cut before", show["name"], len(gone))


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
        if show["kind"] == "book":
            books.extract(show, item)
        elif show["kind"] == "rss":
            podcasts.download(show["feed_url"], item["item_id"], show["id"],
                              hold_for_sponsors=bool(show["skip_sponsors"]))
        else:
            downloader.download(item["url"], show["mode"], show_id=show["id"],
                                min_duration=show["min_duration"], hold_for_sponsors=bool(show["skip_sponsors"]))
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
    push.resolve(f"failed:{show['id']}:{item['item_id']}")
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
        if n and show["kind"] == "youtube":
            time.sleep(pause_seconds())  # go easy on YouTube
        try:
            done += _download_item(show, item)
        except downloader.RateLimited:
            log.warning("Stopping: YouTube is rate-limiting this machine; try again later.")
            break
    return done


def download_next(show_ids: list[int] | None = None,
                  kinds: tuple[str, ...] = ("youtube", "rss", "book")) -> str | bool:
    """Download the oldest queued item that's due, across all enabled shows of
    the given kinds (or only the given shows). Returns the kind of show it was
    for ("youtube" ones need pacing), or False when there was nothing to do."""
    now = _now()
    only = ""
    params: list = [now]
    if show_ids is not None:
        only = f" AND s.id IN ({','.join('?' * len(show_ids))})" if show_ids else " AND 0"
        params += list(show_ids)
    only += f" AND s.kind IN ({','.join('?' * len(kinds))})" if kinds else " AND 0"
    params += list(kinds)
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
    return show["kind"]


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
                WHERE e.mode = 'audio' AND s.mode = 'audio' AND s.video_copy = 1 AND s.enabled = 1
                  AND s.kind = 'youtube'{only}
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
        downloader.download(ep["source_url"], "video", show_id=show["id"],
                            hold_for_sponsors=bool(show["skip_sponsors"]))
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
    # Its transcript (shared by both copies) goes with the last one.
    if not conn.execute("SELECT 1 FROM episodes WHERE video_id = ?", (ep["video_id"],)).fetchone():
        transcribe.path_for(ep["video_id"]).unlink(missing_ok=True)
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
    """Check enabled shows (or the given ones) for new episodes. Returns how many
    were queued. Books are only looked at again when asked to (a part added to
    the folder later): their files may be deleted once the chapters are in."""
    with db.connect() as conn:
        if show_ids is None:
            shows = [dict(r) for r in conn.execute("SELECT * FROM shows WHERE enabled = 1 AND kind != 'book'")]
        else:
            marks = ",".join("?" * len(show_ids))
            shows = [dict(r) for r in conn.execute(
                f"SELECT * FROM shows WHERE id IN ({marks})", show_ids)]
    return sum(_check_feed(show) for show in shows)


def sync(show_id: int | None = None) -> dict:
    """Check feeds, download new episodes and apply retention for one or all shows."""
    with db.connect() as conn:
        if show_id is None:
            shows = [dict(r) for r in conn.execute("SELECT * FROM shows WHERE enabled = 1 AND kind != 'book'")]
        else:
            shows = [dict(r) for r in conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,))]

    totals = {"shows": len(shows), "new": 0, "downloaded": 0, "removed": 0}
    for show in shows:
        totals["new"] += _check_feed(show)
        totals["downloaded"] += _download_queue(show)
        totals["removed"] += _apply_retention(show)
    return totals
