"""Shows followed through their own podcast feed (RSS), all served by the fake web."""

import io
import json

import pytest
from conftest import item
from fastapi.testclient import TestClient

from wren import admin, db, podcasts, server, sponsors, subscriptions, worker
from wren.config import MEDIA_DIR

FEED = "https://feeds.example.com/show.xml"
YT = "https://www.youtube.com/playlist?list=UUtest"


def rss(*entries, title="Example Show"):
    items = "".join(f"""
    <item>
      <title>{e['title']}</title>
      <guid>{e['guid']}</guid>
      <pubDate>{e.get('date', 'Mon, 19 Oct 2026 12:00:00 GMT')}</pubDate>
      <enclosure url="https://cdn.example.com/{e['guid']}.mp3" type="audio/mpeg" length="3"/>
      <itunes:duration>{e.get('duration', '1:02:03')}</itunes:duration>
      <itunes:episodeType>{e.get('type', 'full')}</itunes:episodeType>
      <description>About {e['title']}</description>
    </item>""" for e in entries)
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
  <title>{title}</title><itunes:author>Example Network</itunes:author>
  <itunes:image href="https://cdn.example.com/art.jpg"/>{items}
</channel></rss>""".encode()


def jpeg():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def feed(web):
    web.pages[FEED] = rss({"guid": "ep-2", "title": "Episode two"}, {"guid": "ep-1", "title": "Episode one"},
                          {"guid": "trailer", "title": "Trailer", "type": "trailer"})
    web.pages["https://cdn.example.com/*"] = b"ID3"
    web.pages["https://cdn.example.com/art.jpg"] = jpeg()
    return web


def episodes():
    with db.connect() as conn:
        return {r["video_id"]: dict(r) for r in conn.execute("SELECT * FROM episodes")}


def test_parse_and_fetch(feed):
    f = podcasts.fetch(FEED)
    assert f.title == "Example Show"
    assert [i.title for i in f.items] == ["Episode two", "Episode one"]  # the trailer is left out
    first = f.items[0]
    assert first.item_id == podcasts.item_id("ep-2") and first.item_id.startswith("pod") and len(first.item_id) == 19
    assert (first.duration, first.published, first.url) == (3723, "20261019", "https://cdn.example.com/ep-2.mp3")


@pytest.mark.parametrize("value, seconds", [("3723", 3723), ("62:03", 3723), ("1:02:03", 3723), ("", None),
                                            ("soon", None), ("0", None)])
def test_duration(value, seconds):
    assert podcasts.duration(value) == seconds


def test_finding_a_feed(web):
    web.pages[FEED] = rss()
    assert podcasts.feed_url_for(FEED) == FEED
    web.pages["https://itunes.apple.com/lookup?id=123&entity=podcast"] = json.dumps(
        {"results": [{"feedUrl": FEED}]}).encode()
    assert podcasts.feed_url_for("https://podcasts.apple.com/us/podcast/example/id123") == FEED
    web.pages["https://example.com/page"] = b"<html><body>not a feed"
    with pytest.raises(ValueError, match="isn't a podcast feed"):
        podcasts.feed_url_for("https://example.com/page")
    with pytest.raises(ValueError, match="isn't a podcast feed"):
        podcasts.feed_url_for("https://example.com/missing")  # 404
    web.pages["https://itunes.apple.com/search?*"] = json.dumps({"results": [
        {"collectionName": "Example Show", "artistName": "Example Network", "feedUrl": FEED, "artworkUrl600": "a"},
        {"collectionName": "No feed listed"}]}).encode()
    assert podcasts.search("example") == [
        {"title": "Example Show", "author": "Example Network", "feed_url": FEED, "image": "a"}]


def test_subscribe_and_download(feed):
    show = subscriptions.subscribe(FEED, backfill=1)
    assert (show["kind"], show["feed_url"], show["name"]) == ("rss", FEED, "Example Show")
    assert subscriptions.download_next() == "rss"
    ep = episodes()[podcasts.item_id("ep-2")]
    assert ep["title"] == "Episode two" and ep["file"].endswith(".mp3") and ep["show_id"] == show["id"]
    assert (MEDIA_DIR / ep["file"]).read_bytes() == b"ID3"
    assert ep["thumb"] and (MEDIA_DIR / ep["thumb"]).exists()
    assert (ep["duration"], ep["upload_date"], ep["channel"]) == (3723, "20261019", "Example Network")
    assert ep["published_at"] and ep["description"] == "About Episode two"
    assert subscriptions.download_next() is False  # only the newest was queued


def test_podcast_feeds_are_audio_only(feed):
    with pytest.raises(ValueError, match="audio only"):
        subscriptions.subscribe(FEED, mode="video")
    show = subscriptions.subscribe(FEED)
    with pytest.raises(ValueError, match="audio only"):
        subscriptions.update_show(show["id"], video_copy=True)


def test_switching_source_downloads_nothing_twice(fake_feeds, fake_downloader, feed):
    fake_feeds.set(YT, "Example Show", [item("y1")])
    show = subscriptions.subscribe(YT, backfill=1, keep_last=5)
    subscriptions.sync()
    switched = subscriptions.switch_source(show["id"], FEED)
    assert (switched["kind"], switched["feed_url"], switched["keep_last"]) == ("rss", FEED, 5)
    assert subscriptions.download_next() is False  # everything in the feed counts as seen
    # A new episode in the feed is downloaded; the YouTube one stays in the library.
    feed.pages[FEED] = rss({"guid": "ep-3", "title": "Episode three"}, {"guid": "ep-2", "title": "Episode two"})
    subscriptions.check_feeds()
    assert subscriptions.download_next() == "rss"
    assert set(episodes()) == {"y1", podcasts.item_id("ep-3")}


def test_two_shows_cant_follow_one_feed(fake_feeds, feed):
    subscriptions.subscribe(FEED)
    fake_feeds.set(YT, "Other", [])
    other = subscriptions.subscribe(YT)
    with pytest.raises(ValueError, match="already follows"):
        subscriptions.switch_source(other["id"], FEED)


def test_podcast_downloads_skip_youtube_pacing(feed, monkeypatch):
    subscriptions.subscribe(FEED, backfill=2)
    monkeypatch.setattr(worker, "is_active", lambda ts: False)  # outside download hours
    w = worker.Worker()
    w.next_poll = float("inf")
    w.paused_until = __import__("time").time() + 3600  # and YouTube rate-limited
    assert w._step() and w._step()
    assert len(episodes()) == 2


def test_no_sponsorblock_lookups_for_podcast_episodes(feed, monkeypatch):
    show = subscriptions.subscribe(FEED, backfill=1)
    subscriptions.update_show(show["id"], skip_sponsors=True)
    subscriptions.download_next()
    looked_up = []
    monkeypatch.setattr(sponsors, "lookup", lambda vid: looked_up.append(vid) or [])
    monkeypatch.setattr(sponsors, "cut", lambda path, segs: None)
    sponsors.process_next(now=2_000_000_000)
    assert looked_up == []
    assert episodes()[podcasts.item_id("ep-2")]["sponsor_state"] == "done"


def test_resolve_names_feeds_and_the_same_show(fake_feeds, feed):
    feed.pages["https://itunes.apple.com/search?*"] = json.dumps({"results": [
        {"collectionName": "Example Show", "artistName": "Example Network", "feedUrl": FEED}]}).encode()
    found = admin.resolve("example show")
    assert found["kind"] == "search" and found["results"][0]["feed_url"] == FEED
    fake_feeds.set(YT, "The Example Show", [])
    yt = subscriptions.subscribe(YT)
    r = admin.resolve(FEED)
    assert (r["kind"], r["source"], r["title"]) == ("show", "rss", "Example Show")
    assert r["same_show"] == [{"id": yt["id"], "name": "The Example Show", "kind": "youtube"}]
    assert [i["title"] for i in r["latest"]] == ["Episode two", "Episode one"]


def test_same_show_matching():
    shows = [{"id": 1, "name": "Theo Von", "kind": "youtube"}, {"id": 2, "name": "Hard Fork", "kind": "youtube"},
             {"id": 3, "name": "This Past Weekend", "kind": "rss"}]
    assert [s["id"] for s in admin.same_show(shows, "This Past Weekend w/ Theo Von", "rss")] == [1]
    assert admin.same_show(shows, "Hard Fork", "youtube") == []  # same kind: not a duplicate source


def test_switch_endpoint(fake_feeds, feed):
    client = TestClient(server.app)
    fake_feeds.set(YT, "Example Show", [])
    show = subscriptions.subscribe(YT)
    r = client.post(f"/api/shows/{show['id']}/source", json={"url": FEED})
    assert r.status_code == 200 and r.json()["kind"] == "rss"
    assert client.post("/api/shows/999/source", json={"url": FEED}).status_code == 404
    assert client.post(f"/api/shows/{show['id']}/source", json={"url": "https://example.com/x"}).status_code == 400


def test_podcast_subscribe_ignores_intro_cutting(feed):
    show = subscriptions.subscribe(FEED, skip_sponsors=True, skip_intros=True)
    assert (show["skip_sponsors"], show["skip_intros"]) == (1, 0)  # SponsorBlock marks are YouTube-only


def test_subscribe_api_takes_the_sponsor_settings(fake_feeds):
    client = TestClient(server.app)
    fake_feeds.set(YT, "Example Show", [])
    r = client.post("/api/shows", json={"url": YT, "backfill": 1, "skip_sponsors": True})
    assert r.status_code == 201 and (r.json()["skip_sponsors"], r.json()["skip_intros"]) == (1, 0)
    other = "https://www.youtube.com/playlist?list=UUother"
    fake_feeds.set(other, "Other", [])
    assert client.post("/api/shows", json={"url": other}).json()["skip_sponsors"] == 0  # off by default
