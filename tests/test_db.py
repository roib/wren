import sqlite3

from wren import db
from wren.config import DATA_DIR, DB_PATH, MEDIA_DIR

# The tables as an early version of Wren made them, before the columns that
# _migrate() adds.
OLD_SCHEMA = """
CREATE TABLE episodes (
    video_id TEXT NOT NULL, mode TEXT NOT NULL, title TEXT NOT NULL, channel TEXT, duration INTEGER,
    upload_date TEXT, description TEXT, source_url TEXT NOT NULL, file TEXT NOT NULL, thumb TEXT,
    added_at INTEGER NOT NULL, PRIMARY KEY (video_id, mode));
CREATE TABLE shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, source_url TEXT NOT NULL,
    feed_url TEXT NOT NULL UNIQUE, mode TEXT NOT NULL DEFAULT 'audio', keep_last INTEGER NOT NULL DEFAULT 10,
    min_duration INTEGER NOT NULL DEFAULT 300, title_filter TEXT, enabled INTEGER NOT NULL DEFAULT 1,
    last_checked INTEGER, last_error TEXT, created_at INTEGER NOT NULL);
CREATE TABLE feed_items (
    show_id INTEGER NOT NULL, item_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT, published TEXT,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, first_seen INTEGER NOT NULL,
    PRIMARY KEY (show_id, item_id));
CREATE TABLE alerts (key TEXT PRIMARY KEY);
INSERT INTO episodes VALUES ('abc', 'audio', 'Old episode', NULL, 60, '20200101', NULL,
                             'https://www.youtube.com/watch?v=abc', 'abc.m4a', NULL, 1);
INSERT INTO shows (name, kind, source_url, feed_url, created_at) VALUES ('Old show', 'youtube', 'u', 'f', 1);
INSERT INTO feed_items (show_id, item_id, url, status, first_seen) VALUES (1, 'abc', 'u', 'downloaded', 1);
"""


def columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migrates_old_database_keeping_data():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(DB_PATH)
    old.executescript(OLD_SCHEMA)
    old.close()

    with db.connect() as conn:
        assert {"show_id", "subtitles"} <= columns(conn, "episodes")
        assert "video_copy" in columns(conn, "shows")
        assert {"next_try", "duration", "thumb_url"} <= columns(conn, "feed_items")
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'alerts'").fetchone() is None
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT title FROM episodes").fetchone()["title"] == "Old episode"
        show = conn.execute("SELECT * FROM shows").fetchone()
        assert (show["name"], show["video_copy"]) == ("Old show", 0)
        assert conn.execute("SELECT next_try FROM feed_items").fetchone()["next_try"] == 0


def test_every_schema_column_survives_migration():
    # A column added to SCHEMA but not to _migrate() would be missing here.
    fresh = sqlite3.connect(":memory:")
    fresh.row_factory = sqlite3.Row
    fresh.executescript(db.SCHEMA)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(DB_PATH)
    old.executescript(OLD_SCHEMA)
    old.close()
    with db.connect() as conn:
        for table in ("episodes", "shows", "feed_items"):
            assert columns(conn, table) == columns(fresh, table), table


def test_upsert_and_list_episodes():
    db.upsert_episode({"video_id": "a", "mode": "audio", "title": "A", "source_url": "u", "file": "a.m4a",
                       "added_at": 10})
    db.upsert_episode({"video_id": "b", "mode": "video", "title": "B", "source_url": "u", "file": "b.mp4",
                       "added_at": 20})
    db.upsert_episode({"video_id": "a", "mode": "audio", "title": "A again", "source_url": "u", "file": "a.m4a",
                       "added_at": 30})
    assert [(e["video_id"], e["title"]) for e in db.list_episodes()] == [("a", "A again"), ("b", "B")]
    assert db.has_episode("b", "video")
    assert not db.has_episode("b", "audio")


def test_download_log_is_seeded_once_from_the_library():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(DB_PATH)
    old.executescript(OLD_SCHEMA)
    old.close()
    with db.connect() as conn:
        assert [tuple(r) for r in conn.execute("SELECT video_id, mode, title, at FROM download_log")] == [
            ("abc", "audio", "Old episode", 1)]
    db._migrated = False  # as after a restart
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM download_log").fetchone()[0] == 1


def test_downloads_are_logged_and_outlive_the_episode():
    (MEDIA_DIR / "a.m4a").write_bytes(b"x" * 1234)
    db.upsert_episode({"video_id": "a", "mode": "audio", "title": "A", "source_url": "u", "file": "a.m4a",
                       "added_at": 50, "show_id": 3})
    with db.connect() as conn:
        conn.execute("DELETE FROM episodes")
        rows = [dict(r) for r in conn.execute("SELECT video_id, mode, show_id, title, bytes, at FROM download_log")]
    assert rows == [{"video_id": "a", "mode": "audio", "show_id": 3, "title": "A", "bytes": 1234, "at": 50}]
