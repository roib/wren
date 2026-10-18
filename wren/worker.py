"""Background worker: one thread that does all downloading, one item at a time.

Each pass, in priority order:
  1. check show feeds when a refresh was requested or the poll time has come
  2. run the oldest queued manual "add this URL" job
  3. download the next due subscription item
and otherwise sleeps until something wakes it (a new job, a refresh) or 30s pass.

To stay under YouTube's radar, automatic checks and subscription downloads only
run during ACTIVE_HOURS, poll times are jittered, subscription downloads are
spaced out and capped per hour, and everything pauses for a while when YouTube
starts rate-limiting. Manual adds and refresh requests run whenever asked
(unless paused by rate limiting).
"""

import datetime as dt
import logging
import random
import threading
import time
from collections import deque

from . import db, downloader, subscriptions
from .config import (ACTIVE_HOURS, BLOCK_PAUSE_MINUTES, MAX_DOWNLOADS_PER_HOUR,
                     POLL_JITTER_MINUTES, POLL_MINUTES, TIMEZONE)

log = logging.getLogger("wren")

FIRST_POLL_DELAY = 15  # seconds after startup before the first feed check
IDLE_WAIT = 30
JOB_HISTORY_DAYS = 7


def _now() -> int:
    return int(time.time())


# ---- active hours --------------------------------------------------------------

def is_active(ts: float) -> bool:
    start, end = ACTIVE_HOURS
    if (start, end) in ((0, 24), (0, 0)):
        return True
    hour = dt.datetime.fromtimestamp(ts, TIMEZONE).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


def next_active(ts: float) -> float:
    """`ts` if it's within active hours, otherwise when the next active period starts."""
    if is_active(ts):
        return ts
    local = dt.datetime.fromtimestamp(ts, TIMEZONE)
    start = local.replace(hour=ACTIVE_HOURS[0], minute=0, second=0, microsecond=0)
    if start <= local:
        start += dt.timedelta(days=1)
    return start.timestamp()


# ---- manual jobs ---------------------------------------------------------------

def add_job(url: str, mode: str) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (url, mode, status, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?)",
            (url, mode, _now(), _now()))
        return cur.lastrowid


def list_jobs(limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _update_job(job_id: int, **fields) -> None:
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def _run_next_job() -> bool:
    """Run the oldest queued manual job. Raises RateLimited (leaving it queued)."""
    with db.connect() as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
    if not job:
        return False
    _update_job(job["id"], status="running")
    log.info("Downloading %s (%s)", job["url"], job["mode"])
    try:
        ep = downloader.download(job["url"], job["mode"])
    except downloader.RateLimited as e:
        _update_job(job["id"], status="queued", error=str(e)[:500])
        raise
    except Exception as e:
        _update_job(job["id"], status="error", error=str(e)[:500])
        log.warning("Download failed: %s", e)
    else:
        _update_job(job["id"], status="done", title=ep["title"], error=None)
    return True


def _recover() -> None:
    """Tidy up after a restart: requeue interrupted jobs, release claimed items,
    and drop old finished jobs."""
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
        conn.execute("UPDATE feed_items SET next_try = 0 WHERE status = 'queued' AND error IS NULL")
        conn.execute("DELETE FROM jobs WHERE status IN ('done', 'error') AND updated_at < ?",
                     (_now() - JOB_HISTORY_DAYS * 86400,))


# ---- worker ----------------------------------------------------------------------

class Worker:
    def __init__(self) -> None:
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._refresh: set[int | None] = set()  # show ids to check; None means all
        self._thread: threading.Thread | None = None
        self._recent: deque[float] = deque()  # start times of downloads in the last hour
        self.next_poll = 0.0
        self.last_poll: float | None = None
        self.next_download_at = 0.0  # earliest time for the next subscription download
        self.paused_until = 0.0  # set when YouTube rate-limits us
        self.activity = "idle"

    def start(self) -> None:
        _recover()
        self.next_poll = next_active(time.time() + FIRST_POLL_DELAY)
        self._thread = threading.Thread(target=self._run, name="wren-worker", daemon=True)
        self._thread.start()
        log.info("Worker started; checking shows every %d min between %02d:00 and %02d:00 %s",
                 POLL_MINUTES, *ACTIVE_HOURS, TIMEZONE.key)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def nudge(self) -> None:
        """Something was queued; don't wait for the idle timeout."""
        self._wake.set()

    def refresh(self, show_id: int | None = None) -> None:
        """Check one show (or all) for new episodes as soon as possible."""
        with self._lock:
            self._refresh.add(show_id)
        self._wake.set()

    def status(self) -> dict:
        now = time.time()
        return {
            "activity": self.activity,
            "last_poll": int(self.last_poll) if self.last_poll else None,
            "next_poll": int(self.next_poll),
            "poll_minutes": POLL_MINUTES,
            "active_hours": list(ACTIVE_HOURS),
            "timezone": TIMEZONE.key,
            "active_now": is_active(now),
            "resume_at": int(next_active(now)),
            "paused_until": int(self.paused_until) if self.paused_until > now else None,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._step():
                    continue
            except Exception:
                log.exception("Worker error")
            self._wake.wait(timeout=IDLE_WAIT)
            self._wake.clear()

    def _schedule_next_poll(self) -> None:
        jitter = random.uniform(-POLL_JITTER_MINUTES, POLL_JITTER_MINUTES) * 60
        target = time.time() + POLL_MINUTES * 60 + jitter
        if not is_active(target):
            # Pushed to the start of the next active period: spread that out too.
            target = next_active(target) + random.uniform(0, POLL_JITTER_MINUTES) * 60
        self.next_poll = target

    def _hourly_cap_reached(self, now: float) -> bool:
        while self._recent and self._recent[0] < now - 3600:
            self._recent.popleft()
        return len(self._recent) >= MAX_DOWNLOADS_PER_HOUR

    def _step(self) -> bool:
        """Do one unit of work. Returns False when there's nothing to do right now."""
        now = time.time()
        with self._lock:
            requested, self._refresh = self._refresh, set()
        due = now >= self.next_poll
        if due or requested:
            self.activity = "checking shows"
            if due or None in requested:
                subscriptions.check_feeds()
                self.last_poll = time.time()
            else:
                subscriptions.check_feeds(sorted(requested))
            if due:
                self._schedule_next_poll()
            return True

        if now < self.paused_until:
            self.activity = "paused"
            return False

        try:
            self.activity = "downloading"
            if _run_next_job():
                self._recent.append(now)
                self.next_download_at = time.time() + subscriptions.pause_seconds()
                return True

            if not is_active(now):
                self.activity = "resting"
                return False
            if self._hourly_cap_reached(now):
                self.activity = "hourly limit"
                return False
            if now < self.next_download_at:
                self.activity = "waiting"
                return False
            if subscriptions.download_next():
                self._recent.append(now)
                self.next_download_at = time.time() + subscriptions.pause_seconds()
                return True
        except downloader.RateLimited:
            self.paused_until = time.time() + BLOCK_PAUSE_MINUTES * 60
            self.activity = "paused"
            log.warning("YouTube is rate-limiting us; pausing downloads for %d min",
                        BLOCK_PAUSE_MINUTES)
            return False

        self.activity = "idle"
        return False
