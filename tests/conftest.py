"""Shared test setup: a throwaway WREN_DATA, and fakes for everything that
would talk to YouTube or push services, so the suite never touches the network."""

import os
import shutil
import tempfile
import urllib.parse

# Settings are read when wren.config is imported, so point them at a temp
# directory before anything imports wren.
os.environ["WREN_DATA"] = tempfile.mkdtemp(prefix="wren-test-")
os.environ["WREN_WORKER"] = "0"
os.environ.pop("WREN_BASE_URL", None)
os.environ.pop("ANTHROPIC_API_KEY", None)  # transcript mode only where a test turns it on

import pytest  # noqa: E402

from wren import artwork, db, downloader, feeds, podcast, push, subscriptions  # noqa: E402
from wren.config import DATA_DIR, MEDIA_DIR  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_data(monkeypatch):
    """An empty database and media directory for every test."""
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    MEDIA_DIR.mkdir(parents=True)
    # A stand-in for the placeholder episode, so feeds don't need ffmpeg.
    placeholder = MEDIA_DIR / podcast.PLACEHOLDER
    placeholder.parent.mkdir(parents=True)
    placeholder.write_bytes(b"silence")
    monkeypatch.setattr(db, "_migrated", False)
    monkeypatch.setattr(push, "_vapid", None)
    monkeypatch.setattr(subscriptions, "current_item", None)
    subscriptions._copy_failures.clear()
    # Downloads one after another, without the polite pause.
    monkeypatch.setattr(subscriptions, "pause_seconds", lambda: 0)
    # Channel pictures come from YouTube; tests don't make show artwork.
    monkeypatch.setattr(artwork, "make_show_art", lambda show, fallback_thumb=None: False)
    yield
    shutil.rmtree(DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Push notifications that would have been sent, instead of sending them."""
    out = []
    monkeypatch.setattr(push, "send", lambda title, body, url="/#/status", tag=None: out.append(
        {"title": title, "body": body, "url": url, "tag": tag}))
    return out


def item(video_id, title=None, duration=600, live_status=None, published=None):
    return feeds.FeedItem(
        item_id=video_id, url=f"https://www.youtube.com/watch?v={video_id}",
        title=title or f"Episode {video_id}", duration=duration, live_status=live_status,
        thumb_url=feeds.thumb_url(video_id), published=published)


class FakeFeeds:
    """Show listings set by the test, keyed by feed URL. Any YouTube URL is its own feed URL."""

    def __init__(self):
        self.listings: dict[str, feeds.Feed] = {}
        self.errors: dict[str, Exception] = {}

    def set(self, url, title, items):
        self.listings[url] = feeds.Feed(title=title, items=items)

    def feed_url_for(self, url):
        if "youtube.com" not in url:
            raise ValueError("only YouTube channel and playlist URLs are supported for now")
        return url

    def fetch(self, url):
        if url in self.errors:
            raise self.errors[url]
        return self.listings[url]


@pytest.fixture
def fake_feeds(monkeypatch):
    fake = FakeFeeds()
    monkeypatch.setattr(feeds, "feed_url_for", fake.feed_url_for)
    monkeypatch.setattr(feeds, "fetch", fake.fetch)
    return fake


class FakeDownloader:
    """Records downloads and adds them to the library with a small file, like
    the real one. Set `fail[video_id]` to an exception to make one fail."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, Exception] = {}
        self.upload_dates: dict[str, str] = {}
        self.published: dict[str, int] = {}  # exact publish times; none by default

    def download(self, url, mode="audio", show_id=None, min_duration=None, hold_for_sponsors=False):
        vid = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("v", [url])[0]
        self.calls.append((vid, mode))
        if vid in self.fail:
            raise self.fail[vid]
        ext = "m4a" if mode == "audio" else "mp4"
        (MEDIA_DIR / f"{vid}.{ext}").write_bytes(b"x" * 100)
        (MEDIA_DIR / f"{vid}.jpg").write_bytes(b"jpg")
        ep = {"video_id": vid, "mode": mode, "title": f"Episode {vid}", "channel": "Test Channel",
              "duration": 600, "upload_date": self.upload_dates.get(vid, "20260101"),
              "description": None, "source_url": url, "file": f"{vid}.{ext}", "thumb": f"{vid}.jpg",
              "show_id": show_id, "published_at": self.published.get(vid),
              "sponsor_state": "pending" if hold_for_sponsors else None}
        db.upsert_episode(ep)
        return ep


@pytest.fixture
def fake_downloader(monkeypatch):
    fake = FakeDownloader()
    monkeypatch.setattr(downloader, "download", fake.download)
    return fake
