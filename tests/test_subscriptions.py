import pytest
from conftest import item

from wren import db, downloader, push, subscriptions
from wren.config import MAX_ATTEMPTS, MEDIA_DIR

SHOW = "https://www.youtube.com/playlist?list=UUtest"


def statuses(show_id):
    with db.connect() as conn:
        return {r["item_id"]: r["status"] for r in conn.execute(
            "SELECT item_id, status FROM feed_items WHERE show_id = ?", (show_id,))}


def library():
    return sorted((e["video_id"], e["mode"]) for e in db.list_episodes())


def test_subscribe_queues_backfill_and_skips_the_rest(fake_feeds):
    fake_feeds.set(SHOW, "Test Channel", [
        item("live", live_status="is_live"),
        item("e5"), item("short", duration=30), item("e4"), item("e3"), item("e2"), item("e1"),
    ])
    show = subscriptions.subscribe(SHOW, backfill=2)
    assert show["name"] == "Test Channel"
    # Newest two are queued; the Short is skipped; live streams wait for a later check.
    assert statuses(show["id"]) == {"e5": "queued", "short": "skipped", "e4": "queued",
                                    "e3": "skipped", "e2": "skipped", "e1": "skipped"}
    with pytest.raises(ValueError, match="already subscribed"):
        subscriptions.subscribe(SHOW)


def test_subscribe_validates(fake_feeds):
    with pytest.raises(ValueError, match="mode"):
        subscriptions.subscribe(SHOW, mode="mp3")
    with pytest.raises(ValueError, match="title filter"):
        subscriptions.subscribe(SHOW, title_filter="(")
    with pytest.raises((ValueError, OSError)):  # not a podcast feed, nor reachable
        subscriptions.subscribe("https://example.com/feed")


def test_title_filter(fake_feeds):
    fake_feeds.set(SHOW, "Show", [item("a", title="Full Episode 2"), item("b", title="Clip"),
                                  item("c", title="full episode 1")])
    show = subscriptions.subscribe(SHOW, title_filter="full episode", backfill=10)
    assert statuses(show["id"]) == {"a": "queued", "b": "skipped", "c": "queued"}


def test_check_feed_queues_only_new_items(fake_feeds):
    fake_feeds.set(SHOW, "Show", [item("e1")])
    show = subscriptions.subscribe(SHOW)
    fake_feeds.set(SHOW, "Show", [item("e3"), item("e2", duration=10), item("e1"), item("up", live_status="is_upcoming")])
    assert subscriptions.check_feeds() == 1
    assert statuses(show["id"]) == {"e1": "queued", "e2": "skipped", "e3": "queued"}
    with db.connect() as conn:
        order = [r["item_id"] for r in conn.execute("SELECT item_id FROM feed_items ORDER BY rowid")]
    assert order == ["e1", "e2", "e3"]  # oldest first


def test_check_feed_error_alerts_until_it_works_again(fake_feeds, sent):
    fake_feeds.set(SHOW, "Show", [])
    show = subscriptions.subscribe(SHOW)
    fake_feeds.errors[SHOW] = RuntimeError("ERROR: [youtube:tab] UUtest: This playlist does not exist")
    subscriptions.check_feeds()
    subscriptions.check_feeds()
    assert subscriptions.get_show(show["id"])["last_error"].startswith("feed: ")
    assert [s["body"] for s in sent] == ["This playlist does not exist"]  # once, not every check
    del fake_feeds.errors[SHOW]
    subscriptions.check_feeds()
    assert subscriptions.get_show(show["id"])["last_error"] is None
    assert push.active_count() == 0


def test_sync_downloads_and_keeps_last(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Show", [item("e3"), item("e2"), item("e1")])
    fake_downloader.upload_dates = {"e1": "20260101", "e2": "20260102", "e3": "20260103"}
    show = subscriptions.subscribe(SHOW, keep_last=2, backfill=3)
    totals = subscriptions.sync()
    assert totals["downloaded"] == 3
    assert [c[0] for c in fake_downloader.calls] == ["e1", "e2", "e3"]
    assert library() == [("e2", "audio"), ("e3", "audio")]
    assert not (MEDIA_DIR / "e1.m4a").exists()
    assert not (MEDIA_DIR / "e1.jpg").exists()
    assert statuses(show["id"])["e1"] == "downloaded"  # never fetched again


def test_lower_keep_last_applies_at_once(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Show", [item("e2"), item("e1")])
    fake_downloader.upload_dates = {"e1": "20260101", "e2": "20260102"}
    show = subscriptions.subscribe(SHOW, keep_last=0)
    subscriptions.sync()
    subscriptions.update_show(show["id"], keep_last=1)
    assert library() == [("e2", "audio")]


def test_failed_download_backs_off_then_gives_up(fake_feeds, fake_downloader, sent):
    fake_feeds.set(SHOW, "Show", [item("bad")])
    show = subscriptions.subscribe(SHOW)
    fake_downloader.fail["bad"] = RuntimeError("ERROR: [youtube] bad: Something broke")
    for n in range(1, MAX_ATTEMPTS + 1):
        with db.connect() as conn:
            conn.execute("UPDATE feed_items SET next_try = 0")  # skip the wait
        assert subscriptions.download_next()
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM feed_items").fetchone()
        assert row["attempts"] == n
    assert row["status"] == "failed"
    assert [s["title"] for s in sent] == ["Show: download failed"]
    assert not subscriptions.download_next()  # nothing left to do

    assert subscriptions.requeue_item(show["id"], "bad")
    assert push.active_count() == 0
    del fake_downloader.fail["bad"]
    assert subscriptions.download_next()
    assert statuses(show["id"]) == {"bad": "downloaded"}


def test_retry_waits_for_backoff(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Show", [item("bad")])
    subscriptions.subscribe(SHOW)
    fake_downloader.fail["bad"] = RuntimeError("boom")
    assert subscriptions.download_next()
    assert subscriptions.download_next() is False
    assert len(fake_downloader.calls) == 1


@pytest.mark.parametrize("error, status, error_text", [
    (downloader.Unavailable("private video"), "skipped", "private video"),
    (downloader.Skipped("too short"), "skipped", "too short"),
    (downloader.Skipped("upcoming"), "queued", "upcoming"),
])
def test_skips(fake_feeds, fake_downloader, error, status, error_text):
    fake_feeds.set(SHOW, "Show", [item("x")])
    show = subscriptions.subscribe(SHOW)
    fake_downloader.fail["x"] = error
    subscriptions.download_next()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM feed_items").fetchone()
    assert (row["status"], row["error"], row["attempts"]) == (status, error_text, 0)
    assert subscriptions.get_show(show["id"])


def test_rate_limit_stops_without_using_an_attempt(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Show", [item("b"), item("a")])
    subscriptions.subscribe(SHOW)
    fake_downloader.fail["a"] = downloader.RateLimited("HTTP Error 429")
    totals = subscriptions.sync()
    assert totals["downloaded"] == 0
    assert fake_downloader.calls == [("a", "audio")]  # stopped before "b"
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM feed_items WHERE item_id = 'a'").fetchone()
    assert (row["status"], row["attempts"], row["next_try"]) == ("queued", 0, 0)


def test_episode_already_in_library_is_claimed(fake_feeds, fake_downloader):
    fake_downloader.download("https://www.youtube.com/watch?v=x")  # added by hand
    fake_downloader.calls.clear()
    fake_feeds.set(SHOW, "Show", [item("x")])
    show = subscriptions.subscribe(SHOW)
    subscriptions.download_next()
    assert fake_downloader.calls == []
    assert db.list_episodes()[0]["show_id"] == show["id"]


def test_disabled_shows_are_not_downloaded(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Show", [item("x")])
    show = subscriptions.subscribe(SHOW)
    subscriptions.update_show(show["id"], enabled=False)
    assert subscriptions.download_next() is False
    assert subscriptions.download_next([show["id"]]) is False


def test_video_copies(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Show", [item("x")])
    show = subscriptions.subscribe(SHOW)
    subscriptions.sync()
    assert subscriptions.download_next_copy() is False
    subscriptions.update_show(show["id"], video_copy=True)
    assert subscriptions.download_next_copy()
    assert library() == [("x", "audio"), ("x", "video")]
    assert subscriptions.download_next_copy() is False


def test_delete_episode_keeps_it_from_coming_back(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Show", [item("x")])
    show = subscriptions.subscribe(SHOW, mode="audio")
    subscriptions.update_show(show["id"], video_copy=True)
    subscriptions.sync()
    subscriptions.download_next_copy()
    assert subscriptions.delete_episode("x") == 2
    assert library() == []
    assert not any(MEDIA_DIR.glob("x.*"))
    assert statuses(show["id"]) == {"x": "skipped"}
    assert subscriptions.delete_episode("x") == 0


def test_update_show_validates(fake_feeds):
    fake_feeds.set(SHOW, "Show", [])
    show = subscriptions.subscribe(SHOW)
    with pytest.raises(ValueError, match="can't change"):
        subscriptions.update_show(show["id"], feed_url="x")
    with pytest.raises(ValueError):
        subscriptions.update_show(show["id"], mode="mp3")
    with pytest.raises(KeyError):
        subscriptions.update_show(999, name="x")
    assert subscriptions.update_show(show["id"], name="Renamed")["name"] == "Renamed"


def test_unsubscribe_keeps_episodes_as_singles(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Show", [item("x")])
    show = subscriptions.subscribe(SHOW)
    subscriptions.sync()
    subscriptions.unsubscribe(show["id"])
    assert subscriptions.list_shows() == []
    assert db.list_episodes()[0]["show_id"] is None
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM feed_items").fetchone()[0] == 0


def test_list_pending_states(fake_feeds, fake_downloader, monkeypatch):
    fake_feeds.set(SHOW, "Show", [item("d"), item("c"), item("b"), item("a")])
    show = subscriptions.subscribe(SHOW, backfill=4)
    with db.connect() as conn:
        conn.execute("UPDATE feed_items SET error = 'upcoming' WHERE item_id = 'b'")
        conn.execute("UPDATE feed_items SET error = 'boom' WHERE item_id = 'c'")
    monkeypatch.setattr(subscriptions, "current_item", (show["id"], "a"))
    states = {p["item_id"]: p["state"] for p in subscriptions.list_pending()}
    assert states == {"a": "downloading", "b": "premiere", "c": "retrying", "d": "queued"}
