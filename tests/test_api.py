import xml.etree.ElementTree as ET

import pytest
from conftest import item
from fastapi.testclient import TestClient

from wren import db, push, server, subscriptions

SHOW = "https://www.youtube.com/playlist?list=UUtest"


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def show(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Test Show", [item("e2"), item("e1")])
    s = subscriptions.subscribe(SHOW)
    subscriptions.sync()
    return s


def test_overview_empty(client):
    r = client.get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["shows"] == [] and body["pending"] == [] and body["jobs"] == []
    assert body["singles"]["feed_path"] == "/feeds/singles.xml"
    assert body["worker"]["activity"] == "idle"
    assert r.headers["cache-control"] == "no-cache"


def test_app_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_shows_and_detail(client, show):
    shows = client.get("/api/shows").json()
    assert [(s["name"], s["episodes"], s["feed_path"]) for s in shows] == [
        ("Test Show", 2, "/feeds/test-show.xml")]
    assert shows[0]["video_feed_path"] == "/feeds/test-show-video.xml"
    detail = client.get(f"/api/shows/{show['id']}/detail").json()
    assert {e["video_id"] for e in detail["entries"]} == {"e1", "e2"}
    assert detail["entries"][0]["copies"][0]["media_url"].startswith("/media/")
    assert client.get("/api/shows/999/detail").status_code == 404


def test_subscribe_via_api(client, fake_feeds, monkeypatch):
    monkeypatch.setattr(subscriptions, "ensure_art", lambda show: None)
    fake_feeds.set(SHOW, "New Show", [item("a")])
    r = client.post("/api/shows", json={"url": SHOW, "backfill": 1})
    assert r.status_code == 201
    assert r.json()["name"] == "New Show"
    assert client.post("/api/shows", json={"url": SHOW}).status_code == 400
    assert client.post("/api/shows", json={"url": "https://example.com"}).status_code == 400
    fake_feeds.errors["https://www.youtube.com/playlist?list=UUdown"] = RuntimeError("network down")
    r = client.post("/api/shows", json={"url": "https://www.youtube.com/playlist?list=UUdown"})
    assert r.status_code == 502


def test_update_and_unsubscribe(client, show):
    r = client.patch(f"/api/shows/{show['id']}", json={"keep_last": 1, "enabled": False})
    assert r.status_code == 200
    assert (r.json()["keep_last"], r.json()["enabled"]) == (1, 0)
    assert client.patch(f"/api/shows/{show['id']}", json={"mode": "mp3"}).status_code == 400
    assert client.patch("/api/shows/999", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/shows/{show['id']}").status_code == 204
    assert client.delete(f"/api/shows/{show['id']}").status_code == 404
    assert len(client.get("/api/singles").json()["entries"]) == 1  # what retention left


def test_show_feeds(client, show):
    for path in ("/feeds/test-show.xml", f"/feeds/show/{show['id']}.xml"):
        r = client.get(path, headers={"x-forwarded-proto": "https", "host": "wren.example"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/rss+xml")
        ch = ET.fromstring(r.text).find("channel")
        assert ch.findtext("title") == "Test Show"
        urls = [i.find("enclosure").get("url") for i in ch.findall("item")]
        assert sorted(urls) == ["https://wren.example/media/e1.m4a", "https://wren.example/media/e2.m4a"]
    # No video copies yet: just the placeholder.
    video = ET.fromstring(client.get("/feeds/test-show-video.xml").text).find("channel")
    assert video.findtext("title") == "Test Show (video)"
    assert [i.findtext("guid") for i in video.findall("item")] == ["wren-placeholder"]
    assert client.get("/feeds/nope.xml").status_code == 404
    assert client.get("/feeds/show/999.xml").status_code == 404


def test_singles_art_is_ready_on_first_load(client):
    # The singles page may be opened before anything else has made the artwork.
    art = client.get("/api/singles").json()["art_url"]
    assert art.startswith("/media/shows/singles.jpg?v=")
    assert client.get(art).status_code == 200


def test_recent_downloads(client, show, fake_downloader):
    fake_downloader.download("https://www.youtube.com/watch?v=s1")  # a single
    fake_downloader.download("https://www.youtube.com/watch?v=e1", "video", show_id=show["id"])
    with db.connect() as conn:
        for vid, mode, when in (("e1", "audio", 100), ("e2", "audio", 300), ("s1", "audio", 200),
                                ("e1", "video", 400)):
            conn.execute("UPDATE episodes SET added_at = ? WHERE video_id = ? AND mode = ?", (when, vid, mode))
    entries = client.get("/api/recent").json()["entries"]
    # Newest download first; the video copy of e1 brings it to the top.
    assert [(e["video_id"], e["show"], e["show_id"]) for e in entries] == [
        ("e1", "Test Show", show["id"]), ("e2", "Test Show", show["id"]), ("s1", "Single episodes", None)]
    assert [c["mode"] for c in entries[0]["copies"]] == ["audio", "video"]
    overview = client.get("/api/overview").json()
    assert [e["video_id"] for e in overview["recent"]] == ["e1", "e2", "s1"]
    assert overview["recent_count"] == 0  # all "downloaded" long ago
    with db.connect() as conn:
        conn.execute("UPDATE episodes SET added_at = strftime('%s') WHERE video_id = 'e2'")
    assert client.get("/api/overview").json()["recent_count"] == 1


def test_singles_feed(client, fake_downloader):
    fake_downloader.download("https://www.youtube.com/watch?v=s1")
    ch = ET.fromstring(client.get("/feeds/singles.xml").text).find("channel")
    assert [i.findtext("title") for i in ch.findall("item")] == ["Episode s1"]


def test_media_is_served_with_ranges(client, show):
    r = client.get("/media/e1.m4a", headers={"range": "bytes=0-9"})
    assert r.status_code == 206
    assert r.headers["content-type"] == "audio/mp4"
    assert len(r.content) == 10
    assert "cache-control" not in r.headers


def test_delete_and_requeue_episode(client, show):
    assert client.delete("/api/episodes/e1").status_code == 204
    assert client.delete("/api/episodes/e1").status_code == 404
    assert client.post(f"/api/shows/{show['id']}/items/e1/queue").status_code == 202
    assert client.post(f"/api/shows/{show['id']}/items/e1/queue").status_code == 404  # queued already


def test_add_job(client):
    r = client.post("/api/add", json={"url": "https://www.youtube.com/watch?v=abc"})
    assert r.status_code == 202
    jobs = client.get("/api/jobs").json()
    assert jobs[r.json()["job_id"]]["status"] == "queued"
    assert client.post("/api/add", json={"url": "u", "mode": "flac"}).status_code == 400


def test_resolve_rejects_other_sites(client):
    r = client.post("/api/resolve", json={"url": "https://vimeo.com/123"})
    assert r.status_code == 400


def test_alerts(client):
    push.alert("a", "Something", "happened")
    alerts = client.get("/api/alerts").json()["active"]
    assert [a["title"] for a in alerts] == ["Something"]
    assert client.get("/api/overview").json()["alerts"] == 1
    assert client.post(f"/api/alerts/{alerts[0]['id']}/dismiss").status_code == 204
    assert client.get("/api/alerts").json()["active"] == []


def test_push_endpoints(client):
    assert client.get("/api/push/key").json()["devices"] == 0
    assert client.post("/api/push/test").status_code == 400
    sub = {"endpoint": "https://push.example/1", "keys": {"p256dh": "p", "auth": "a"}}
    assert client.post("/api/push/subscribe", json=sub).status_code == 204
    assert client.post("/api/push/subscribe", json={**sub, "keys": {}}).status_code == 400
    assert client.get("/api/push/key").json()["devices"] == 1
    assert client.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]}).status_code == 204
