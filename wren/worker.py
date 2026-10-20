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
import shutil
import threading
import time
from collections import deque
from pathlib import Path

import yt_dlp

from . import ads, db, downloader, push, sponsors, subscriptions, transcribe
from .config import (ACTIVE_HOURS, ADS_BACKEND, BLOCK_PAUSE_MINUTES, MAX_DOWNLOADS_PER_HOUR, MEDIA_DIR,
                     POLL_JITTER_MINUTES, POLL_MINUTES, SPONSOR_DELAY_HOURS, TIMEZONE)

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
        push.alert(f"job:{job['url']}", "Couldn't add an episode", f"{job['url']}: {push.short_error(e)}", "/#/singles")
    else:
        _update_job(job["id"], status="done", title=ep["title"], error=None)
        push.resolve(f"job:{job['url']}")
    return True


def _recover() -> None:
    """Tidy up after a restart: requeue interrupted jobs, release claimed items,
    and drop old finished jobs."""
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
        conn.execute("UPDATE feed_items SET next_try = 0 WHERE status = 'queued' AND error IS NULL")
        conn.execute("DELETE FROM jobs WHERE status IN ('done', 'error') AND updated_at < ?",
                     (_now() - JOB_HISTORY_DAYS * 86400,))


# ---- daily health checks -------------------------------------------------------------

LOW_DISK_BYTES = 50 * 10**9
_checked_on: str | None = None


def daily_checks(now: float) -> None:
    """Once a day, during active hours (so nothing arrives in the night): alert
    about a failed yt-dlp update, low disk space and shows that went quiet."""
    global _checked_on
    today = dt.datetime.fromtimestamp(now, TIMEZONE).date()
    if _checked_on == today.isoformat():
        return
    _checked_on = today.isoformat()
    try:
        # Each check also resolves its earlier alert once the problem is gone.
        if Path("/tmp/ytdlp-update-failed").exists():
            push.alert("ytdlp", "yt-dlp didn't update",
                       f"Wren is using its built-in yt-dlp ({yt_dlp.version.__version__}). "
                       "Downloads may break if YouTube changed something.")
        else:
            push.resolve("ytdlp")
        free = shutil.disk_usage(MEDIA_DIR).free
        if free < LOW_DISK_BYTES:
            push.alert("disk", "Running low on space", f"Only {free / 1e9:.0f} GB left for downloads.")
        else:
            push.resolve("disk")
        quiet = set()
        for show, days, usual in quiet_shows(today):
            key = f"quiet:{show['id']}:{show['newest']}"
            quiet.add(key)
            push.alert(key, f"{show['name']} has gone quiet",
                       f"No new episode in {days} days (usually every {usual}). "
                       "The channel may have moved its full episodes elsewhere.", f"/#/show/{show['id']}")
        push.resolve_matching("quiet:", keep=quiet)
    except Exception:
        log.exception("Daily checks failed")


def quiet_shows(today: dt.date) -> list[tuple[dict, int, str]]:
    """Enabled shows whose newest episode is much older than their usual gap:
    more than 3x the median gap between their episodes, and at least two weeks."""
    out = []
    with db.connect() as conn:
        shows = [dict(r) for r in conn.execute("SELECT id, name FROM shows WHERE enabled = 1")]
        for s in shows:
            dates = sorted({r[0] for r in conn.execute(
                "SELECT upload_date FROM episodes WHERE show_id = ? AND upload_date IS NOT NULL", (s["id"],))})
            if len(dates) < 3:
                continue
            days = [dt.datetime.strptime(d, "%Y%m%d").date() for d in dates]
            gaps = sorted((b - a).days for a, b in zip(days, days[1:]))
            median = max(1, gaps[len(gaps) // 2])
            age = (today - days[-1]).days
            if age > max(14, 3 * median):
                usual = "day" if median == 1 else f"{median} days" if median < 14 else f"{round(median / 7)} weeks"
                out.append(({**s, "newest": dates[-1]}, age, usual))
    return out


def check_helper() -> None:
    """Alert when the host helper that runs `claude -p` stops working (only if it was ever set up)."""
    status = ads.helper_status()
    if status is None or ADS_BACKEND == "api":
        return
    if ads.helper_ok():
        push.resolve("claudehelper")
        return
    if not status.get("ok") and status.get("error"):
        body = f"{status['error']}. Until it's back, sponsor reads wait for SponsorBlock."
    else:
        seen = dt.datetime.fromtimestamp(status.get("at", 0), TIMEZONE).strftime("%a %-I:%M %p")
        body = f"No word from it since {seen}. Until it's back, sponsor reads wait for SponsorBlock."
    push.alert("claudehelper", "Claude on the server isn't answering", body)


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
        # "Download now": ignore active hours until the queue (of all shows, or of
        # the requested ones) is empty.
        self.override_all = False
        self.override_shows: set[int] = set()
        self.activity = "idle"
        # Cutting sponsor reads runs beside downloads, as transcribing an episode
        # takes minutes; it never touches YouTube, so at any hour, even while paused.
        self._processing: threading.Thread | None = None
        self._process_wake = threading.Event()
        self._cleaned_at = 0.0
        self._helper_checked_at = 0.0

    def start(self) -> None:
        _recover()
        push.resolve_matching("paused:")  # a pause doesn't survive a restart
        self.next_poll = next_active(time.time() + FIRST_POLL_DELAY)
        self._thread = threading.Thread(target=self._run, name="wren-worker", daemon=True)
        self._thread.start()
        self._processing = threading.Thread(target=self._process_loop, name="wren-sponsors", daemon=True)
        self._processing.start()
        log.info("Worker started (yt-dlp %s); checking shows every %d min between %02d:00 and %02d:00 %s",
                 yt_dlp.version.__version__, POLL_MINUTES, *ACTIVE_HOURS, TIMEZONE.key)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._process_wake.set()
        for thread in (self._thread, self._processing):
            if thread:
                thread.join(timeout=5)

    def nudge(self) -> None:
        """Something was queued; don't wait for the idle timeout."""
        self._wake.set()

    def refresh(self, show_id: int | None = None) -> None:
        """Check one show (or all) for new episodes as soon as possible."""
        with self._lock:
            self._refresh.add(show_id)
        self._wake.set()

    def download_now(self, show_id: int | None = None) -> None:
        """Check every show (or one), then download what's queued even outside
        active hours. The gap between downloads, the hourly cap and rate-limit
        pauses still apply."""
        if show_id is None:
            self.override_all = True
        else:
            self.override_shows.add(show_id)
        self.next_download_at = 0.0
        self.refresh(show_id)

    def status(self) -> dict:
        now = time.time()
        return {
            "activity": self.activity,
            "ytdlp": yt_dlp.version.__version__,
            "last_poll": int(self.last_poll) if self.last_poll else None,
            "next_poll": int(self.next_poll),
            "poll_minutes": POLL_MINUTES,
            "active_hours": list(ACTIVE_HOURS),
            "sponsor_delay_hours": SPONSOR_DELAY_HOURS,
            "sponsor_mode": sponsors.mode(),
            "ads_backend": ads.backend(),
            "processing": sponsors.current,
            "timezone": TIMEZONE.key,
            "active_now": is_active(now),
            "resume_at": int(next_active(now)),
            "paused_until": int(self.paused_until) if self.paused_until > now else None,
            "override": self.override_all or bool(self.override_shows),
            "override_shows": sorted(self.override_shows),
        }

    def _process_once(self) -> bool:
        """Cut one held-back episode that's due; tidy old transcripts hourly;
        check on the host helper every 10 minutes."""
        if time.time() - self._cleaned_at > 3600:
            self._cleaned_at = time.time()
            transcribe.clean_up()
        if time.time() - self._helper_checked_at > 600:
            self._helper_checked_at = time.time()
            check_helper()
        return sponsors.process_next()

    def _process_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._process_once():
                    continue
            except Exception:
                log.exception("Sponsor processing error")
            self._process_wake.wait(timeout=IDLE_WAIT)
            self._process_wake.clear()

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
        if is_active(now):
            daily_checks(now)
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
        if self.paused_until:
            push.resolve(f"paused:{int(self.paused_until)}")
            self.paused_until = 0.0

        try:
            self.activity = "downloading"
            if _run_next_job():
                self._recent.append(now)
                self.next_download_at = time.time() + subscriptions.pause_seconds()
                return True

            active = is_active(now)
            if not (active or self.override_all or self.override_shows):
                self.activity = "resting"
                return False
            if self._hourly_cap_reached(now):
                self.activity = "hourly limit"
                return False
            if now < self.next_download_at:
                self.activity = "waiting"
                return False
            # Outside active hours with only some shows requested, stick to those.
            only = None if (active or self.override_all) else sorted(self.override_shows)
            if subscriptions.download_next(only) or subscriptions.download_next_copy(only):
                self._process_wake.set()  # it may be one to cut
                self._recent.append(now)
                self.next_download_at = time.time() + subscriptions.pause_seconds()
                return True
            # Nothing left (for the requested shows): back to the normal schedule.
            self.override_shows.clear()
            if only is None:
                self.override_all = False
        except downloader.RateLimited:
            self.paused_until = time.time() + BLOCK_PAUSE_MINUTES * 60
            self.activity = "paused"
            log.warning("YouTube is rate-limiting us; pausing downloads for %d min",
                        BLOCK_PAUSE_MINUTES)
            resume = dt.datetime.fromtimestamp(self.paused_until, TIMEZONE).strftime("%-I:%M %p")
            push.alert(f"paused:{int(self.paused_until)}", "Downloads paused",
                       f"YouTube is rate-limiting the server. Wren will try again at {resume}.")
            return False

        self.activity = "idle"
        return False
