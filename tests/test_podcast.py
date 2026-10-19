import shutil
import xml.etree.ElementTree as ET

import pytest

from wren import podcast
from wren.config import MEDIA_DIR

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def episode(video_id, **fields):
    return {"video_id": video_id, "mode": "audio", "title": f"Episode {video_id}", "channel": "Test Channel",
            "duration": 3725, "upload_date": "20260301", "description": None,
            "source_url": f"https://www.youtube.com/watch?v={video_id}", "file": f"{video_id}.m4a",
            "thumb": f"{video_id}.jpg", "added_at": 1_700_000_000, **fields}


def channel(xml):
    return ET.fromstring(xml).find("channel")


def test_slug():
    assert podcast.slug("Andrew Huberman") == "andrew-huberman"
    assert podcast.slug("  The Daily: News!  ") == "the-daily-news"
    assert podcast.slug("???") == "show"


def test_feed_slugs_clashes_and_reserved_names():
    shows = [{"id": 3, "name": "Singles"}, {"id": 1, "name": "My Show"}, {"id": 2, "name": "my show!"}]
    # The older show keeps the plain name; names Wren uses itself get the id.
    assert podcast.feed_slugs(shows) == {1: "my-show", 2: "my-show-2", 3: "singles-3"}


def test_render_items_newest_first():
    (MEDIA_DIR / "old.m4a").write_bytes(b"x" * 42)
    eps = [episode("old", upload_date="20260101"), episode("new", upload_date="20260201")]
    ch = channel(podcast.render("Show", "About it", eps, "https://wren.example", "https://wren.example/feeds/show.xml"))
    items = ch.findall("item")
    assert [i.findtext("title") for i in items] == ["Episode new", "Episode old"]
    enclosure = items[1].find("enclosure")
    assert enclosure.get("url") == "https://wren.example/media/old.m4a"
    assert enclosure.get("length") == "42"
    assert enclosure.get("type") == "audio/mp4"
    assert items[1].findtext(f"{ITUNES}duration") == "1:02:05"
    assert items[1].findtext("guid") == "wren-old-audio"


def test_render_channel_tags():
    eps = [episode("a", description="Tom & Jerry <live>")]
    ch = channel(podcast.render("Tom & Jerry", "Desc", eps, "https://wren.example", "https://wren.example/f.xml",
                                art="shows/1.jpg?v=5"))
    assert ch.findtext("title") == "Tom & Jerry"
    assert ch.findtext(f"{ITUNES}block") == "Yes"  # private feeds stay out of directories
    assert ch.findtext(f"{ITUNES}author") == "Test Channel"
    assert ch.find(f"{ITUNES}image").get("href") == "https://wren.example/media/shows/1.jpg?v=5"
    assert ch.find("item").findtext("description") == "Tom & Jerry <live>"


def test_render_art_defaults_to_newest_thumbnail():
    eps = [episode("old", upload_date="20260101"), episode("new", upload_date="20260201")]
    ch = channel(podcast.render("Show", "", eps, "https://w", "https://w/f.xml"))
    assert ch.find(f"{ITUNES}image").get("href") == "https://w/media/new.jpg"


def test_pub_date_falls_back_to_added_at():
    assert podcast._pub_date(episode("a", upload_date=None)).timestamp() == 1_700_000_000
    assert podcast._pub_date(episode("a", upload_date="bad")).timestamp() == 1_700_000_000


def test_empty_feed_gets_placeholder():
    # Apple Podcasts won't follow a feed without episodes.
    items = channel(podcast.render("Show", "", [], "https://w", "https://w/f.xml")).findall("item")
    assert len(items) == 1
    assert items[0].findtext("guid") == "wren-placeholder"
    assert items[0].find("enclosure").get("url") == f"https://w/media/{podcast.PLACEHOLDER}"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_placeholder_is_made_with_ffmpeg():
    path = MEDIA_DIR / podcast.PLACEHOLDER
    path.unlink()
    podcast.render("Show", "", [], "https://w", "https://w/f.xml")
    assert path.stat().st_size > 1000
