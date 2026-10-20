"""Audiobooks from the books folder: made with ffmpeg here, so they need it installed."""

import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from wren import admin, books, db, server, subscriptions
from wren.config import BOOKS_DIR, MEDIA_DIR

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def make_part(path, seconds, chapters=(), title="Test Book", artist="Jane Author", cover=False):
    """An AAC file with tags, chapter marks [(start, end, title)] and maybe a cover."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = path.with_suffix(".txt")
    meta.write_text(";FFMETADATA1\n" + f"album={title}\nartist={artist}\n" + "".join(
        f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(a * 1000)}\nEND={int(b * 1000)}\ntitle={t}\n" for a, b, t in chapters))
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=d={seconds}", "-i", str(meta)]
    if cover:
        cmd += ["-f", "lavfi", "-i", "color=c=red:s=60x90:d=0.04"]  # one frame
    cmd += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    if cover:
        cmd += ["-map", "2:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
    subprocess.run(cmd + ["-c:a", "aac", "-b:a", "32k", "-f", "ipod", str(path)], check=True)
    meta.unlink()


@pytest.fixture
def book():
    """A book in two files: the first with two chapters and a cover, the second without chapter marks."""
    make_part(BOOKS_DIR / "Test Book" / "Part 2.m4b", 3)
    make_part(BOOKS_DIR / "Test Book" / "Part 1.m4b", 6, [(0, 2, "Opening Credits"), (2, 6, "Chapter One")],
              cover=True)
    (BOOKS_DIR / "Test Book" / "notes.txt").write_text("not audio")
    return "book:Test Book"


def chapters(show_id):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM episodes WHERE show_id = ? ORDER BY number", (show_id,))]


def length(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout)


def test_reading_a_book(book):
    b = books.read(book)
    assert (b["title"], b["author"], b["parts"]) == ("Test Book", "Jane Author", 2)
    assert [(c.number, c.title, c.part) for c in b["chapters"]] == [
        (1, "Opening Credits", "Test Book/Part 1.m4b"), (2, "Chapter One", "Test Book/Part 1.m4b"),
        (3, "Part 2", "Test Book/Part 2.m4b")]  # no chapter marks: the file is one chapter
    assert books.cover(book) and (MEDIA_DIR / books.cover(book)).exists()


def test_parts_in_natural_order(tmp_path):
    names = ["Part 10.m4b", "Part 2.m4b", "part 1.m4b"]
    assert sorted(names, key=books._natural) == ["part 1.m4b", "Part 2.m4b", "Part 10.m4b"]


def test_only_the_books_folder(book):
    with pytest.raises(ValueError, match="books folder"):
        books.source_for("book:../wren.db")
    with pytest.raises(ValueError, match="no such file"):
        books.source_for("book:Missing")
    assert books.source_for("Test Book/") == book


def test_adding_a_book_cuts_every_chapter(book):
    show = subscriptions.subscribe(book, keep_last=3, backfill=1)
    assert (show["kind"], show["name"], show["author"], show["keep_last"], show["min_duration"]) == (
        "book", "Test Book", "Jane Author", 0, 0)
    while subscriptions.download_next():
        pass
    eps = chapters(show["id"])
    assert [(e["number"], e["title"]) for e in eps] == [(1, "Opening Credits"), (2, "Chapter One"), (3, "Part 2")]
    assert [round(length(MEDIA_DIR / e["file"])) for e in eps] == [2, 4, 3]
    assert [e["duration"] for e in eps] == [2, 4, 3]
    assert all(e["thumb"] == eps[0]["thumb"] and e["channel"] == "Jane Author" for e in eps)
    assert eps[0]["published_at"] < eps[1]["published_at"] < eps[2]["published_at"]

    feed = TestClient(server.app).get(f"/feeds/show/{show['id']}.xml")
    channel = ET.fromstring(feed.content).find("channel")
    assert channel.findtext(f"{ITUNES}type") == "serial"
    assert [(i.findtext("title"), i.findtext(f"{ITUNES}episode")) for i in channel.findall("item")] == [
        ("Part 2", "3"), ("Chapter One", "2"), ("Opening Credits", "1")]

    detail = admin.show_detail(show["id"])
    assert [e["title"] for e in detail["entries"]] == ["Opening Credits", "Chapter One", "Part 2"]  # reading order


def test_books_settings_and_removal(book):
    show = subscriptions.subscribe(book)
    while subscriptions.download_next():
        pass
    with pytest.raises(ValueError, match="only its name"):
        subscriptions.update_show(show["id"], skip_sponsors=True)
    with pytest.raises(ValueError, match="can't be switched"):
        subscriptions.switch_source(show["id"], "https://feeds.example.com/show.xml")
    assert subscriptions.update_show(show["id"], name="Renamed")["name"] == "Renamed"
    files = [MEDIA_DIR / e["file"] for e in chapters(show["id"])]
    subscriptions.unsubscribe(show["id"])
    assert db.list_episodes() == [] and not any(f.exists() for f in files)
    assert (BOOKS_DIR / "Test Book" / "Part 1.m4b").exists()  # the originals stay


def test_books_are_read_again_only_when_asked(book):
    show = subscriptions.subscribe(book)
    while subscriptions.download_next():
        pass
    make_part(BOOKS_DIR / "Test Book" / "Part 3.m4b", 2)
    assert subscriptions.check_feeds() == 0  # the regular checks leave books alone
    assert subscriptions.check_feeds([show["id"]]) == 1
    subscriptions.download_next()
    assert [e["title"] for e in chapters(show["id"])][-1] == "Part 3"


def test_books_folder_and_api(book):
    make_part(BOOKS_DIR / "Single.m4b", 2, title="Single File Book")
    client = TestClient(server.app)
    listing = client.get("/api/books").json()["books"]
    assert [(b["name"], b["parts"], b["added"]) for b in listing] == [("Single", 1, None), ("Test Book", 2, None)]
    preview = client.post("/api/resolve", json={"url": book}).json()
    assert (preview["kind"], preview["title"], len(preview["chapters"]), preview["duration"]) == ("book", "Test Book", 3, 9)
    show = client.post("/api/shows", json={"url": "book:Single.m4b"}).json()
    assert (show["kind"], show["name"]) == ("book", "Single File Book")
    assert client.get("/api/books").json()["books"][0]["added"] == {"id": show["id"], "name": "Single File Book"}
    assert client.post("/api/shows", json={"url": "book:Single.m4b"}).status_code == 400  # already added


def test_parts_that_each_number_their_chapters(book):
    make_part(BOOKS_DIR / "Saga" / "Saga (Part 1 of 2).m4b", 4, [(0, 2, "Part 01"), (2, 4, "Part 02")],
              title="Saga 1: The Start 1 of 2")
    make_part(BOOKS_DIR / "Saga" / "Saga (Part 2 of 2).m4b", 4, [(0, 2, "Part 01"), (2, 4, "Part 02")],
              title="Saga 1: The Start 2 of 2")
    b = books.read("book:Saga")
    assert b["title"] == "Saga 1: The Start"
    assert [c.title for c in b["chapters"]] == ["Part 1", "Part 2", "Part 3", "Part 4"]


@pytest.mark.parametrize("tag, title", [("The Long Road 1 of 2", "The Long Road"), ("Dune (Part 2 of 3)", "Dune"),
                                        ("Dune, Part 2", "Dune"), ("Catch-22", "Catch-22"), ("1984", "1984")])
def test_book_title_without_its_part(tag, title):
    assert books._book_title(tag) == title


def test_reading_again_renames_chapters(book, monkeypatch):
    show = subscriptions.subscribe(book)
    while subscriptions.download_next():
        pass
    real = books.read
    monkeypatch.setattr(books, "read", lambda s: {**(b := real(s)), "chapters": [
        books.Chapter(c.number, c.title.upper(), c.part, c.start, c.end) for c in b["chapters"]]})
    subscriptions.check_feeds([show["id"]])
    assert [e["title"] for e in chapters(show["id"])] == ["OPENING CREDITS", "CHAPTER ONE", "PART 2"]


# ---- chapters from the epub -----------------------------------------------------------

def make_epub(path, chapters, title="The Epub Title", author="E. Author"):
    """A minimal EPUB 2: [(toc title, text)] in reading order."""
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   "</rootfiles></container>")
        items = "".join(f'<item id="c{n}" href="Text/c{n}.html" media-type="application/xhtml+xml"/>'
                        for n in range(len(chapters)))
        z.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata>"
                   f'<manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>{items}</manifest>'
                   "</package>")
        points = "".join(f'<navPoint id="p{n}"><navLabel><text>{t}</text></navLabel><content src="Text/c{n}.html"/>'
                         "</navPoint>" for n, (t, _) in enumerate(chapters))
        z.writestr("OEBPS/toc.ncx", '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
                   f'version="2005-1"><navMap>{points}</navMap></ncx>')
        for n, (t, text) in enumerate(chapters):
            z.writestr(f"OEBPS/Text/c{n}.html", f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>{t}</h1>'
                       f"<p>{text}</p></body></html>")


def text(name, n=400):
    return " ".join(f"{name}{i}" for i in range(n))


def spoken(*runs):
    """A fake Whisper transcript: [(start, end, text)] runs, words spread evenly over each."""
    segs = []
    for start, end, said in runs:
        ws = said.split()
        step = (end - start) / len(ws)
        segs.append({"start": start, "end": end, "text": said,
                     "words": [[start + i * step, start + (i + 1) * step, w] for i, w in enumerate(ws)]})
    return {"segments": segs, "seconds": 0}


@pytest.fixture
def epub_book(monkeypatch):
    """Two files; the epub's prologue and chapter 1 in the first, a part page and
    chapters 2 and 4 in the second; chapter 3 and the back matter nowhere in the audio."""
    from wren import transcribe
    make_part(BOOKS_DIR / "Epub Book" / "Part 1.m4b", 6, [(0, 3, "Part 01"), (3, 6, "Part 02")])
    make_part(BOOKS_DIR / "Epub Book" / "Part 2.m4b", 6, [(0, 6, "Part 01")])
    make_epub(BOOKS_DIR / "Epub Book" / "book.epub", [
        ("Copyright", "all rights reserved"), ("Prologue", text("pro")), ("Chapter 1: First", text("one")),
        ("Part II: Later", text("parttwo", 20)), ("Chapter 2: Second", text("two")),
        ("Chapter 3: Third", text("three")), ("Chapter 4: Fourth", text("four")), ("About the Author", text("bio"))])
    heard = {"Part 1.m4b": spoken((0.2, 0.6, "presents an audiobook"), (1.0, 2.6, text("pro", 80)),
                                  (3.6, 5.8, text("one", 80))),
             "Part 2.m4b": spoken((0.2, 0.8, "part two of two"), (2.0, 2.5, text("parttwo", 20)),
                                  (2.8, 3.9, text("two", 80)), (4.6, 5.8, text("four", 80)))}
    calls = []
    monkeypatch.setattr(transcribe, "available", lambda: True)
    monkeypatch.setattr(transcribe, "transcribe", lambda path: calls.append(path.name) or heard[path.name])
    return calls


def test_epub_chapters_are_found_in_the_audio(epub_book):
    source = "book:Epub Book"
    before = books.read(source)
    assert (before["title"], before["author"], before["placing"], before["chapters"]) == (
        "The Epub Title", "E. Author", True, [])
    assert before["contents"] == ["Prologue", "Chapter 1: First", "Chapter 2: Second", "Chapter 3: Third",
                                  "Chapter 4: Fourth", "About the Author"]
    assert books.needs_placing(source) and books.fetch(source).items == []

    books.place_chapters(source)
    assert not books.needs_placing(source)
    chs = books.read(source)["chapters"]
    # Chapter 3 wasn't found: it's in chapter 2. The back matter isn't anywhere.
    assert [c.title for c in chs] == ["Prologue", "Chapter 1: First",
                                      "Part II: Later · Chapter 2: Second & Chapter 3: Third", "Chapter 4: Fourth"]
    # The credits before the prologue go with it; chapter 1 runs on into the second file.
    assert (chs[0].part, chs[0].start) == ("Epub Book/Part 1.m4b", 0.0)
    assert chs[1].part.endswith("Part 1.m4b") and 2.6 <= chs[1].start <= 3.6 and chs[1].end == pytest.approx(6, abs=0.1)
    assert [(p, s) for p, s, _ in chs[1].more] == [("Epub Book/Part 2.m4b", 0.0)] and chs[1].more[0][2] == pytest.approx(0.8)
    assert chs[2].part.endswith("Part 2.m4b") and chs[2].start == pytest.approx(0.8) and not chs[2].more
    assert chs[3].end == pytest.approx(6, abs=0.1)
    assert epub_book == ["Part 1.m4b", "Part 2.m4b"]
    books.place_chapters(source)  # again: the transcripts were kept
    assert epub_book == ["Part 1.m4b", "Part 2.m4b"]


def test_epub_chapters_replace_the_ones_from_marks(epub_book, monkeypatch):
    from wren import transcribe
    monkeypatch.setattr(transcribe, "available", lambda: False)  # no Whisper yet: the files' marks
    show = subscriptions.subscribe("book:Epub Book")
    while subscriptions.download_next():
        pass
    assert [e["title"] for e in chapters(show["id"])] == ["Part 1", "Part 2", "Part 3"]
    old = [MEDIA_DIR / e["file"] for e in chapters(show["id"])]

    monkeypatch.setattr(transcribe, "available", lambda: True)
    books.place_chapters(show["feed_url"])
    subscriptions.check_feeds([show["id"]])
    while subscriptions.download_next():
        pass
    eps = chapters(show["id"])
    # The epub's author replaces the one from the files' tags.
    assert subscriptions.get_show(show["id"])["author"] == "E. Author"
    assert {e["channel"] for e in eps} == {"E. Author"}
    assert [e["title"] for e in eps] == ["Prologue", "Chapter 1: First",
                                         "Part II: Later · Chapter 2: Second & Chapter 3: Third", "Chapter 4: Fourth"]
    assert not any(f.exists() for f in old)
    # Chapter 1 was joined from both files: its length is both pieces'.
    assert round(length(MEDIA_DIR / eps[1]["file"]), 1) == pytest.approx(eps[1]["duration"], abs=1)
    assert length(MEDIA_DIR / eps[1]["file"]) > 6 - 3.6 + 0.5


def test_too_few_epub_chapters_found_keeps_the_marks(epub_book, monkeypatch):
    from wren import transcribe
    monkeypatch.setattr(transcribe, "transcribe", lambda path: spoken((0.5, 1.0, "nothing from the book")))
    result = books.place_chapters("book:Epub Book")
    assert result["chapters"] == [] and "0 of 6" in result["note"]
    assert [c.title for c in books.read("book:Epub Book")["chapters"]] == ["Part 1", "Part 2", "Part 3"]


def test_worker_places_a_books_chapters(epub_book):
    from wren import worker
    show = subscriptions.subscribe("book:Epub Book")
    assert subscriptions.download_next() is False  # nothing until its chapters are found
    w = worker.Worker()
    assert w._place_next_book() and w._refresh == {show["id"]}
    subscriptions.check_feeds([show["id"]])
    assert len(chapters(show["id"])) == 0 and subscriptions.download_next() == "book"
    assert w._place_next_book() is False
