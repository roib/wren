"""Audiobooks: a book is a show, each of its chapters an episode.

A book is a file (.m4b, .m4a or .mp3) or a folder of them in the books folder
(WREN_BOOKS). Parts in a folder play in file-name order ("Part 2" before
"Part 10"); each part's chapters, from its own chapter marks, become episodes
in turn, and a part without chapter marks is one episode. Chapters are cut out
losslessly (stream copy), so the original files can be deleted afterwards.
Its feed is "serial": podcast apps list it first chapter first.
"""

import hashlib
import json
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import BOOKS_DIR, MEDIA_DIR
from .feeds import Feed, FeedItem

PREFIX = "book:"  # a book's source and feed "URL": book:<path in the books folder>
AUDIO = (".m4b", ".m4a", ".mp3")
COVERS = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png")


def is_book(url: str) -> bool:
    return url.strip().startswith(PREFIX)


def _natural(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _resolve(rel: str) -> Path:
    """A path inside the books folder; anything outside it is refused."""
    root = BOOKS_DIR.resolve()
    path = (root / rel.strip().strip("/")).resolve()
    if path != root and root not in path.parents:
        raise ValueError("Books have to be in Wren's books folder.")
    return path


def _parts(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in AUDIO else []
    if path.is_dir():
        return sorted((p for p in path.iterdir() if p.is_file() and p.suffix.lower() in AUDIO),
                      key=lambda p: _natural(p.name))
    return []


def source_for(text: str) -> str:
    """The book: source for a pasted path ("book:…", or a path in the books folder)."""
    rel = text.strip().removeprefix(PREFIX).strip()
    path = _resolve(rel)
    if not _parts(path):
        raise ValueError("That isn't a book: no .m4b, .m4a or .mp3 files there." if path.exists()
                         else "There's no such file or folder in the books folder.")
    return PREFIX + path.relative_to(BOOKS_DIR.resolve()).as_posix()


def available() -> list[dict]:
    """What's in the books folder, for the Add page: each audio file and each
    folder holding some, with the show it was added as (if it was)."""
    if not BOOKS_DIR.is_dir():
        return []
    with db.connect() as conn:
        added = {r["feed_url"]: {"id": r["id"], "name": r["name"]}
                 for r in conn.execute("SELECT id, name, feed_url FROM shows WHERE kind = 'book'")}
    out = []
    for p in sorted(BOOKS_DIR.iterdir(), key=lambda p: _natural(p.name)):
        parts = _parts(p)
        if not parts or p.name.startswith("."):
            continue
        source = PREFIX + p.name
        out.append({"source": source, "name": p.stem if p.is_file() else p.name, "parts": len(parts),
                    "bytes": sum(f.stat().st_size for f in parts), "added": added.get(source)})
    return out


# ---- reading a book ---------------------------------------------------------------

@dataclass
class Chapter:
    number: int  # 1-based, across all parts
    title: str
    part: str  # the file, relative to the books folder
    start: float
    end: float
    marked: bool = True  # from a chapter mark (not the whole file)


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_chapters",
                          "-show_streams", str(path)], capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"can't read {path.name}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def _tags(info: dict) -> dict:
    return {k.lower(): v for k, v in (info.get("format", {}).get("tags") or {}).items()}


# "… 1 of 2", "… (Part 1 of 2)", "… Part 1": which file of the book this is.
PART_SUFFIX = re.compile(r"[\s,:;\-–(\[]*(?:part|pt\.?|disc|disk)?\s*\d+\s*(?:of|/)\s*\d+[)\]]?$"
                         r"|[\s,:;\-–(\[]+(?:part|pt\.?|disc|disk)\s*\d+[)\]]?$", re.I)
# Chapter names that are only a number ("Part 01", "Chapter 3", "Track 12", "07").
NUMBERED = re.compile(r"^(part|chapter|track|disc|disk)?[\s._-]*0*(\d+)$", re.I)


def _book_title(tag: str) -> str:
    return PART_SUFFIX.sub("", tag).strip() or tag


def _renumber(chapters: list["Chapter"]) -> None:
    """Parts of a book often each number their chapters from 1 ("Part 01" in
    both files): number those on through the whole book instead. (Only names
    from chapter marks; a part without marks is named after its file.)"""
    n = 0
    for ch in chapters:
        m = NUMBERED.match(ch.title) if ch.marked else None
        if m:
            n += 1
            ch.title = f"{(m.group(1) or 'Chapter').capitalize()} {n}"


def read(source: str) -> dict:
    """A book's title, author and chapters, from its files' tags and chapter marks."""
    path = _resolve(source.removeprefix(PREFIX))
    parts = _parts(path)
    if not parts:
        raise RuntimeError("the book's files are gone from the books folder")
    root = BOOKS_DIR.resolve()
    chapters, title, author = [], None, None
    for part in parts:
        info = _probe(part)
        tags = _tags(info)
        title = title or tags.get("album") or tags.get("title")
        author = author or tags.get("artist") or tags.get("album_artist") or tags.get("composer")
        rel = part.relative_to(root).as_posix()
        marks = [(float(c["start_time"]), float(c["end_time"]), (c.get("tags") or {}).get("title"))
                 for c in info.get("chapters") or []]
        marked = bool(marks)
        if not marks:  # the whole part is one chapter
            length = float(info.get("format", {}).get("duration") or 0)
            marks = [(0.0, length, tags.get("title") if len(parts) > 1 else None)]
        for start, end, name in marks:
            if end - start < 0.5:
                continue  # a stray zero-length mark
            n = len(chapters) + 1
            chapters.append(Chapter(n, (name or "").strip() or (part.stem if len(parts) > 1 and len(marks) == 1
                                                                else f"Chapter {n}"), rel, start, end, marked))
    if len(parts) > 1:
        _renumber(chapters)
    title = title or (path.stem if path.is_file() else path.name)
    return {"title": _book_title(title) if len(parts) > 1 else title, "author": author,
            "chapters": chapters, "parts": len(parts)}


def item_id(ch: Chapter) -> str:
    """Stable per chapter (its file and start), never mistaken for a YouTube id."""
    return "book" + hashlib.sha256(f"{ch.part}#{ch.start:.3f}".encode()).hexdigest()[:16]


def _item_url(ch: Chapter) -> str:
    return PREFIX + urllib.parse.quote(ch.part) + "#" + urllib.parse.urlencode(
        {"n": ch.number, "t": f"{ch.start:.3f},{ch.end:.3f}"})


def fetch(source: str) -> Feed:
    """The book as a show listing, newest (last chapter) first like any feed."""
    book = read(source)
    thumb = cover(source)
    items = [FeedItem(item_id=item_id(ch), url=_item_url(ch), title=ch.title, duration=round(ch.end - ch.start),
                      live_status=None, thumb_url=f"/media/{thumb}" if thumb else None)
             for ch in reversed(book["chapters"])]
    return Feed(title=book["title"], items=items)


# ---- cover -----------------------------------------------------------------------------

COVER_DIR = MEDIA_DIR / "books"


def _cover_path(source: str) -> Path:
    return COVER_DIR / f"{hashlib.sha256(source.encode()).hexdigest()[:12]}.jpg"


def cover(source: str) -> str | None:
    """The book's cover as a JPEG in the media folder (relative to it): the
    picture embedded in its first part that has one, else a cover image in its
    folder. Made once."""
    out = _cover_path(source)
    if out.exists():
        return out.relative_to(MEDIA_DIR).as_posix()
    path = _resolve(source.removeprefix(PREFIX))
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    for part in _parts(path):
        pics = [s for s in _probe(part).get("streams", []) if (s.get("disposition") or {}).get("attached_pic")]
        if pics and not subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(part), "-map", f"0:{pics[0]['index']}",
                                        "-frames:v", "1", str(tmp)], capture_output=True).returncode:
            tmp.replace(out)
            return out.relative_to(MEDIA_DIR).as_posix()
    folder = path if path.is_dir() else path.parent
    for name in COVERS:
        if (folder / name).is_file():
            from PIL import Image
            Image.open(folder / name).convert("RGB").save(out, "JPEG", quality=88)
            return out.relative_to(MEDIA_DIR).as_posix()
    return None


# ---- cutting a chapter out -------------------------------------------------------------

def extract(show: dict, item: dict) -> dict:
    """Cut one chapter out of its part (no re-encoding) and add it to the library."""
    part, _, fragment = item["url"].removeprefix(PREFIX).partition("#")
    src = _resolve(urllib.parse.unquote(part))
    if not src.is_file():
        raise RuntimeError("the book's file is gone from the books folder")
    q = urllib.parse.parse_qs(fragment)
    number = int(q["n"][0])
    start, end = (float(x) for x in q["t"][0].split(","))
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = MEDIA_DIR / f"{item['item_id']}{'.mp3' if src.suffix.lower() == '.mp3' else '.m4a'}"
    tmp = path.with_name(path.stem + ".part" + path.suffix)
    run = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src),
                          "-t", f"{end - start:.3f}", "-map", "0:a:0", "-c", "copy", "-map_chapters", "-1",
                          "-metadata", f"title={item['title']}", "-movflags", "+faststart", str(tmp)],
                         capture_output=True, text=True)
    if run.returncode:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"couldn't cut the chapter out: {run.stderr.strip()[:200]}")
    tmp.replace(path)
    book = show["name"]
    episode = {
        "video_id": item["item_id"], "mode": "audio", "title": item["title"],
        "channel": show.get("author"), "duration": round(end - start),
        # In chapter order, a second apart: podcast apps order a serial feed by date.
        "upload_date": None, "published_at": show["created_at"] + number,
        "description": f"{book}, chapter {number}.", "source_url": item["url"],
        "file": path.relative_to(MEDIA_DIR).as_posix(), "thumb": cover(show["feed_url"]),
        "show_id": show["id"], "number": number,
    }
    db.upsert_episode(episode)
    return episode
