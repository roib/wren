"""Parsing pasted links, without looking anything up on YouTube."""

import pytest

from wren import admin, feeds

CHANNEL = "UC" + "a" * 22


@pytest.mark.parametrize("url, feed_url", [
    (f"https://www.youtube.com/channel/{CHANNEL}", "https://www.youtube.com/playlist?list=UU" + "a" * 22),
    (f"https://www.youtube.com/channel/{CHANNEL}/videos", "https://www.youtube.com/playlist?list=UU" + "a" * 22),
    ("https://www.youtube.com/playlist?list=PLxyz", "https://www.youtube.com/playlist?list=PLxyz"),
    ("https://youtube.com/watch?v=abc&list=PLxyz", "https://www.youtube.com/playlist?list=PLxyz"),
])
def test_feed_url_for(url, feed_url):
    assert feeds.feed_url_for(url) == feed_url


def test_feed_url_for_rejects():
    with pytest.raises(ValueError, match="only YouTube"):
        feeds.feed_url_for("https://vimeo.com/channels/x")
    with pytest.raises(ValueError, match="not a channel"):
        feeds.feed_url_for("https://www.youtube.com/feed/subscriptions")


@pytest.mark.parametrize("url, is_video", [
    ("https://www.youtube.com/watch?v=abc", True),
    ("https://youtu.be/abc", True),
    ("https://www.youtube.com/shorts/abc", True),
    ("https://www.youtube.com/live/abc", True),
    ("https://www.youtube.com/watch?v=abc&list=PLxyz", True),
    ("https://www.youtube.com/watch?list=PLxyz", False),
    ("https://www.youtube.com/@someone", False),
    ("https://www.youtube.com/playlist?list=PLxyz", False),
])
def test_is_video(url, is_video):
    assert admin._is_video(url) is is_video


def test_thumb_url():
    assert feeds.thumb_url("abc") == "https://i.ytimg.com/vi/abc/mqdefault.jpg"
