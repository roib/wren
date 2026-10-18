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
    PRIMARY KEY (video_id, mode)
);
"""


@contextmanager
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
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
        rows = conn.execute("SELECT * FROM episodes ORDER BY added_at DESC").fetchall()
    return [dict(r) for r in rows]


def has_episode(video_id: str, mode: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM episodes WHERE video_id = ? AND mode = ?", (video_id, mode)
        ).fetchone()
    return row is not None
