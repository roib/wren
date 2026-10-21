import hashlib
import io
import json
import shutil
import subprocess
import time
import urllib.error
import xml.etree.ElementTree as ET

import pytest
from conftest import item
from fastapi.testclient import TestClient

from wren import db, server, sponsors, subscriptions
from wren.config import MEDIA_DIR, SPONSOR_DELAY_HOURS

SHOW = "https://www.youtube.com/playlist?list=UUtest"
DELAY = SPONSOR_DELAY_HOURS * 3600


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def sponsorblock(monkeypatch):
    """SponsorBlock answers set by the test (video id -> segments), and cuts recorded."""
    marks, cuts = {}, []
    monkeypatch.setattr(sponsors, "lookup", lambda vid: marks.get(vid, []))
    monkeypatch.setattr(sponsors, "cut", lambda path, segs: cuts.append((path.name, segs)))
    sponsors._next_try.clear()
    return marks, cuts


@pytest.fixture
def show(fake_feeds, fake_downloader):
    fake_feeds.set(SHOW, "Test Show", [item("e1")])
    s = subscriptions.subscribe(SHOW, backfill=1)
    subscriptions.update_show(s["id"], skip_sponsors=True)
    fake_downloader.published["e1"] = 1_000_000
    subscriptions.download_next()
    return s


def episode(video_id="e1", mode="audio"):
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM episodes WHERE video_id = ? AND mode = ?", (video_id, mode)).fetchone())


def feed_titles(client, path="/feeds/test-show.xml"):
    return [i.findtext("title") for i in ET.fromstring(client.get(path).text).find("channel").findall("item")]


def test_merge():
    assert sponsors.merge([(50, 60), (10, 20), (19.8, 30), (70, 70.5), (-3, 2)], duration=55) == [
        (0, 2), (10, 30), (50, 55)]


def test_held_back_until_the_delay_after_publishing(client, show, sponsorblock):
    marks, cuts = sponsorblock
    marks["e1"] = [(100, 160), (30, 45)]
    assert episode()["sponsor_state"] == "pending"
    assert feed_titles(client) == ["Waiting for new episodes"]  # the placeholder
    detail = client.get(f"/api/shows/{show['id']}/detail").json()
    assert detail["entries"][0]["sponsors"] == {"state": "pending", "ready_at": 1_000_000 + DELAY}

    # Counted from when YouTube published it, not from the download.
    assert not sponsors.process_next(now=1_000_000 + DELAY - 1)
    assert sponsors.process_next(now=1_000_000 + DELAY)
    assert cuts == [("e1.m4a", [(30, 45), (100, 160)])]
    ep = episode()
    assert (ep["sponsor_state"], ep["sponsor_removed"], ep["duration"]) == ("done", 75, 600 - 75)
    assert json.loads(ep["sponsor_segments"]) == [[30, 45], [100, 160]]
    assert feed_titles(client) == ["Episode e1"]
    description = ET.fromstring(client.get("/feeds/test-show.xml").text).find("channel/item/description").text
    assert description.startswith("Ads removed by Wren (1:15)")
    detail = client.get(f"/api/shows/{show['id']}/detail").json()
    assert detail["entries"][0]["sponsors"] == {
        "state": "done", "removed": 75, "count": 2, "note": None, "description_removed": [],
        "cuts": [{"start": 30, "end": 45, "source": "sponsorblock"}, {"start": 100, "end": 160, "source": "sponsorblock"}]}

    # Processed once: never looked at again.
    marks["e1"] = [(0, 500)]
    assert not sponsors.process_next(now=2_000_000_000)
    assert len(cuts) == 1


def test_no_marks_publishes_it_as_is(client, show, sponsorblock):
    _, cuts = sponsorblock
    sponsors.process_next(now=2_000_000_000)
    assert cuts == []
    assert (episode()["sponsor_state"], episode()["sponsor_removed"], episode()["duration"]) == ("done", 0, 600)
    assert feed_titles(client) == ["Episode e1"]


def test_without_a_publish_time_counts_from_the_download(fake_feeds, fake_downloader, sponsorblock):
    fake_feeds.set(SHOW, "Test Show", [item("e1")])
    s = subscriptions.subscribe(SHOW, backfill=1)
    subscriptions.update_show(s["id"], skip_sponsors=True)
    subscriptions.download_next()
    added = episode()["added_at"]
    assert not sponsors.process_next(now=added + DELAY - 1)
    assert sponsors.process_next(now=added + DELAY)


def test_sponsorblock_down_retries_then_publishes_uncut(show, monkeypatch):
    sponsors._next_try.clear()
    calls = []

    def down(vid):
        calls.append(vid)
        raise urllib.error.URLError("unreachable")
    monkeypatch.setattr(sponsors, "lookup", down)
    due = 1_000_000 + DELAY
    assert sponsors.process_next(now=due)  # tried, will retry
    assert episode()["sponsor_state"] == "pending"
    assert not sponsors.process_next(now=due + 60)  # waiting to retry
    sponsors.process_next(now=due + sponsors.GIVE_UP_SECONDS + 1)
    assert episode()["sponsor_state"] == "done"
    assert len(calls) == 2


def test_shows_without_the_setting_are_untouched(client, fake_feeds, fake_downloader, sponsorblock):
    fake_feeds.set(SHOW, "Test Show", [item("e1")])
    subscriptions.subscribe(SHOW, backfill=1)
    subscriptions.download_next()
    assert episode()["sponsor_state"] is None
    assert feed_titles(client) == ["Episode e1"]
    assert not sponsors.process_next(now=2_000_000_000)


def test_video_copies_are_held_back_too(show, fake_downloader, sponsorblock):
    subscriptions.update_show(show["id"], video_copy=True)
    subscriptions.download_next_copy()
    assert episode(mode="video")["sponsor_state"] == "pending"


def test_setting_via_api(client, fake_feeds):
    fake_feeds.set(SHOW, "Test Show", [])
    s = subscriptions.subscribe(SHOW)
    assert client.patch(f"/api/shows/{s['id']}", json={"skip_sponsors": True}).json()["skip_sponsors"] == 1
    assert client.get("/api/overview").json()["worker"]["sponsor_delay_hours"] == SPONSOR_DELAY_HOURS


def test_lookup_uses_a_hash_prefix_and_picks_the_video(monkeypatch):
    seen = []

    def urlopen(req, timeout):
        seen.append(req.full_url)
        return io.BytesIO(json.dumps([
            {"videoID": "other", "segments": [{"segment": [1, 2], "actionType": "skip"}]},
            {"videoID": "abc", "segments": [{"segment": [5, 9], "actionType": "skip"},
                                            {"segment": [20, 20], "actionType": "poi"}]},
        ]).encode())
    monkeypatch.setattr(sponsors.urllib.request, "urlopen", urlopen)
    assert sponsors.lookup("abc") == [(5, 9)]
    assert seen[0].startswith(sponsors.API + hashlib.sha256(b"abc").hexdigest()[:4] + "?")
    assert "abc" not in seen[0].split("?")[0][-10:]

    def not_found(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(sponsors.urllib.request, "urlopen", not_found)
    assert sponsors.lookup("abc") == []


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_cut_removes_the_segments_without_reencoding():
    path = MEDIA_DIR / "tone.m4a"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
                    "-c:a", "aac", "-b:a", "64k", str(path)], check=True)
    sponsors.cut(path, [(5, 10), (20, 25)])
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    info = json.loads(out.stdout)
    assert abs(float(info["format"]["duration"]) - 20) < 0.3
    assert info["streams"][0]["codec_name"] == "aac"
    assert sorted(p.name for p in MEDIA_DIR.iterdir() if p.is_file()) == ["tone.m4a"]  # no leftovers


def test_feeds_use_the_exact_publish_time():
    from wren import podcast
    ep = {"published_at": 1_700_000_123, "upload_date": "20200101", "added_at": 1}
    assert podcast._pub_date(ep).timestamp() == 1_700_000_123


def test_ready_time_from_publish():
    assert sponsors.ready_at({"published_at": 100, "added_at": 5}) == 100 + DELAY
    assert time.time() < sponsors.ready_at({"published_at": None, "added_at": time.time()})


def test_intro_and_outro_are_cut_when_asked(show, monkeypatch):
    sponsors._next_try.clear()
    monkeypatch.setattr(sponsors, "lookup", lambda vid: [(100.0, 160.0)])
    asked = []

    def categories(vid, cats):
        asked.append(tuple(cats))
        return {"intro": [(2.0, 11.0, "intro")], "outro": [(590.0, 600.0, "outro")]}[cats[0]]
    monkeypatch.setattr(sponsors, "lookup_categories", categories)
    cuts = []
    monkeypatch.setattr(sponsors, "cut", lambda path, segs: cuts.append(segs))
    subscriptions.update_show(show["id"], skip_intros=True)
    sponsors.process_next(now=2_000_000_000)
    decided = json.loads(episode()["sponsor_cuts"])["cuts"]
    assert [(c["start"], c.get("category")) for c in decided] == [(2.0, "intro"), (100.0, None), (590.0, "outro")]
    assert cuts == [[(2.0, 11.0), (100.0, 160.0), (590.0, 600.0)]]
    assert episode()["sponsor_removed"] == 79


def test_intro_marks_only_when_the_show_asks(show, monkeypatch):
    sponsors._next_try.clear()
    monkeypatch.setattr(sponsors, "lookup", lambda vid: [])
    asked = []
    monkeypatch.setattr(sponsors, "lookup_categories", lambda vid, cats: asked.append(cats) or [])
    monkeypatch.setattr(sponsors, "cut", lambda path, segs: None)
    sponsors.process_next(now=2_000_000_000)
    assert asked == []  # setting off: not even looked up


def test_intro_lookup_failing_keeps_the_intro(show, monkeypatch):
    sponsors._next_try.clear()
    monkeypatch.setattr(sponsors, "lookup", lambda vid: [(100.0, 160.0)])

    def down(vid, cats):
        raise urllib.error.URLError("unreachable")
    monkeypatch.setattr(sponsors, "lookup_categories", down)
    monkeypatch.setattr(sponsors, "cut", lambda path, segs: None)
    subscriptions.update_show(show["id"], skip_intros=True)
    sponsors.process_next(now=2_000_000_000)
    assert [c["start"] for c in json.loads(episode()["sponsor_cuts"])["cuts"]] == [100.0]


def test_skip_intros_via_api(client, fake_feeds):
    fake_feeds.set(SHOW, "Test Show", [])
    s = subscriptions.subscribe(SHOW)
    assert client.patch(f"/api/shows/{s['id']}", json={"skip_intros": True}).json()["skip_intros"] == 1
