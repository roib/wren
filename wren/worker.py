"""Background worker: one thread that does all downloading, one item at a time.

Each pass, in priority order:
  1. check show feeds when a refresh was requested or the poll interval is up
  2. run the oldest queued manual "add this URL" job
  3. download the next due subscription item
and otherwise sleeps until something wakes it (a new job, a refresh) or 30s pass.
"""

import logging
import threading
import time

from . import db, downloader, subscriptions
from .config import DOWNLOAD_PAUSE_SECONDS, POLL_MINUTES

log = logging.getLogger("wren")

FIRST_POLL_DELAY = 15  # seconds after startup before the first feed check
IDLE_WAIT = 30
JOB_HISTORY_DAYS = 7


def _now() -> int:
    return int(time.time())


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
    with db.connect() as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
    if not job:
        return False
    _update_job(job["id"], status="running")
    log.info("Downloading %s (%s)", job["url"], job["mode"])
    try:
        ep = downloader.download(job["url"], job["mode"])
    except Exception as e:
        _update_job(job["id"], status="error", error=str(e)[:500])
        log.warning("Download failed: %s", e)
    else:
        _update_job(job["id"], status="done", title=ep["title"])
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
        self.next_poll = 0.0
        self.last_poll: float | None = None
        self.activity = "idle"

    def start(self) -> None:
        _recover()
        self.next_poll = time.time() + FIRST_POLL_DELAY
        self._thread = threading.Thread(target=self._run, name="wren-worker", daemon=True)
        self._thread.start()
        log.info("Worker started; checking shows every %d min", POLL_MINUTES)

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
        return {
            "activity": self.activity,
            "last_poll": int(self.last_poll) if self.last_poll else None,
            "next_poll": int(self.next_poll),
            "poll_minutes": POLL_MINUTES,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._step():
                    continue
            except Exception:
                log.exception("Worker error")
            self.activity = "idle"
            self._wake.wait(timeout=IDLE_WAIT)
            self._wake.clear()

    def _step(self) -> bool:
        """Do one unit of work. Returns False when there was nothing to do."""
        with self._lock:
            requested, self._refresh = self._refresh, set()
        due = time.time() >= self.next_poll
        if due or requested:
            self.activity = "checking shows"
            if due or None in requested:
                subscriptions.check_feeds()
                self.last_poll = time.time()
                self.next_poll = self.last_poll + POLL_MINUTES * 60
            else:
                subscriptions.check_feeds(sorted(requested))
            return True

        self.activity = "downloading"
        if _run_next_job():
            return True
        if subscriptions.download_next():
            self._stop.wait(DOWNLOAD_PAUSE_SECONDS)  # go easy on YouTube
            return True
        return False
