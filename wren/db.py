import sqlite3
import time
from contextlib import contextmanager

from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    video_id    TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('audio', 'video')),
    title       TEXT NOT NULL,
    channel     TEXT,
    duration    INTEGER,
    upload_date TEXT,
    description TEXT,
    source_url  TEXT NOT NULL,
    file        TEXT NOT NULL,   -- path relative to MEDIA_DIR
    thumb       TEXT,            -- path relative to MEDIA_DIR
    added_at    INTEGER NOT NULL,
    show_id     INTEGER,         -- subscription it came from; NULL for manual adds
    subtitles   TEXT,            -- WebVTT file relative to MEDIA_DIR (video episodes)
    PRIMARY KEY (video_id, mode)
);

CREATE TABLE IF NOT EXISTS shows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('youtube', 'rss')),
    source_url   TEXT NOT NULL,
    feed_url     TEXT NOT NULL UNIQUE,
    mode         TEXT NOT NULL DEFAULT 'audio' CHECK (mode IN ('audio', 'video')),
    keep_last    INTEGER NOT NULL DEFAULT 10,   -- 0 keeps everything
    min_duration INTEGER NOT NULL DEFAULT 300,  -- seconds; filters out Shorts
    title_filter TEXT,                          -- optional regex episodes must match
    enabled      INTEGER NOT NULL DEFAULT 1,
    video_copy   INTEGER NOT NULL DEFAULT 0,    -- audio show: also download each episode as video
    last_checked INTEGER,
    last_error   TEXT,
    created_at   INTEGER NOT NULL
);

-- Every item ever seen in a show's feed, so nothing is downloaded twice and
-- skipped or cleaned-up episodes are never fetched again.
CREATE TABLE IF NOT EXISTS feed_items (
    show_id    INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    item_id    TEXT NOT NULL,   -- YouTube video id (or RSS guid)
    url        TEXT NOT NULL,
    title      TEXT,
    published  TEXT,            -- YYYYMMDD upload date, approximate (from the show listing)
    status     TEXT NOT NULL CHECK (status IN ('queued', 'downloaded', 'skipped', 'failed')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    first_seen INTEGER NOT NULL,
    next_try   INTEGER NOT NULL DEFAULT 0,  -- don't attempt before this time (retry backoff)
    duration   INTEGER,         -- seconds, from the show listing
    thumb_url  TEXT,            -- remote thumbnail, shown before the episode is downloaded
    PRIMARY KEY (show_id, item_id)
);

-- Manual "add this URL" downloads, processed by the background worker.
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL,
    mode       TEXT NOT NULL CHECK (mode IN ('audio', 'video')),
    status     TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'error')),
    title      TEXT,
    error      TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

_migrated = False


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring databases created by older versions up to the current schema."""
    global _migrated
    if _migrated:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(episodes)")}
    if "show_id" not in cols:
        conn.execute("ALTER TABLE episodes ADD COLUMN show_id INTEGER")
    if "subtitles" not in cols:
        conn.execute("ALTER TABLE episodes ADD COLUMN subtitles TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(shows)")}
    if "video_copy" not in cols:
        conn.execute("ALTER TABLE shows ADD COLUMN video_copy INTEGER NOT NULL DEFAULT 0")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_items)")}
    if "next_try" not in cols:
        conn.execute("ALTER TABLE feed_items ADD COLUMN next_try INTEGER NOT NULL DEFAULT 0")
    if "duration" not in cols:
        conn.execute("ALTER TABLE feed_items ADD COLUMN duration INTEGER")
    if "thumb_url" not in cols:
        conn.execute("ALTER TABLE feed_items ADD COLUMN thumb_url TEXT")
    # WAL lets the API read while the worker thread writes.
    conn.execute("PRAGMA journal_mode = WAL")
    _migrated = True


@contextmanager
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_episode(ep: dict) -> None:
    ep = {**ep, "added_at": ep.get("added_at") or int(time.time())}
    cols = ", ".join(ep)
    params = ", ".join(f":{k}" for k in ep)
    with connect() as conn:
        conn.execute(f"INSERT OR REPLACE INTO episodes ({cols}) VALUES ({params})", ep)


def list_episodes() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT e.*, s.name AS show FROM episodes e
               LEFT JOIN shows s ON s.id = e.show_id
               ORDER BY e.added_at DESC""").fetchall()
    return [dict(r) for r in rows]


def has_episode(video_id: str, mode: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM episodes WHERE video_id = ? AND mode = ?", (video_id, mode)
        ).fetchone()
    return row is not None
