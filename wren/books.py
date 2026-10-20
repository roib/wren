"""Audiobooks: a book is a show, each of its chapters an episode.

A book is a file (.m4b, .m4a or .mp3) or a folder of them in the books folder
(WREN_BOOKS). Parts in a folder play in file-name order ("Part 2" before
"Part 10"); each part's chapters, from its own chapter marks, become episodes
in turn, and a part without chapter marks is one episode. Chapters are cut out
losslessly (stream copy), so the original files can be deleted afterwards.
Its feed is "serial": podcast apps list it first chapter first.

With the book's .epub beside its audio, the chapters come from the book
instead: the audio is transcribed and each chapter's opening words found in it
(epub.py), which works where the files' own marks are useless ("Part 01" every
75 minutes) and for dramatizations that never read a chapter title aloud. That
takes a while (Whisper, about 40× real time), so it's done in the background
(place_chapters, from the worker) and the book's chapters appear once it's done.
"""

import hashlib
import json
import logging
import re
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import db, epub, transcribe
from .config import BOOKS_DIR, DATA_DIR, MEDIA_DIR
from .feeds import Feed, FeedItem

log = logging.getLogger("wren")

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
    more: list = field(default_factory=list)  # [(part, start, end)]: where it goes on in later files

    @property
    def duration(self) -> float:
        return self.end - self.start + sum(e - s for _, s, e in self.more)


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


def _epub_for(path: Path) -> Path | None:
    """The book's text: an .epub in its folder, or beside a single file with the same name."""
    if path.is_dir():
        return next(iter(sorted(path.glob("*.epub"))), None)
    twin = path.with_suffix(".epub")
    return twin if twin.is_file() else None


def read(source: str) -> dict:
    """A book's title, author and chapters: from its epub's placement in the
    audio where there is one ("placing" while that's still to be done), else
    from its files' tags and chapter marks."""
    path = _resolve(source.removeprefix(PREFIX))
    parts = _parts(path)
    if not parts:
        raise RuntimeError("the book's files are gone from the books folder")
    book = _from_marks(path, parts)
    text = _epub_for(path) if transcribe.available() else None
    if text:
        try:
            info = epub.read(text)
        except Exception as e:
            log.warning("%s: can't use %s: %s", source, text.name, e)
            return book
        book.update(title=info["title"] or book["title"], author=info["author"] or book["author"],
                    epub=text.name, contents=[e.title for e in info["entries"] if len(e.words) >= CHAPTER_WORDS])
        placed = _load_placement(source, _key(text, parts))
        if placed is None:
            book.update(chapters=[], placing=True)
        elif placed.get("chapters"):
            book.update(chapters=[Chapter(n, c["title"], *c["pieces"][0], more=[tuple(p) for p in c["pieces"][1:]])
                                  for n, c in enumerate(placed["chapters"], 1)],
                        unplaced=placed.get("unplaced", []))
        else:
            book["note"] = placed.get("note")  # couldn't be placed: the files' marks it is
    return book


def _from_marks(path: Path, parts: list[Path]) -> dict:
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
            "chapters": chapters, "parts": len(parts), "duration": round(sum(c.duration for c in chapters))}


def item_id(ch: Chapter) -> str:
    """Stable per chapter (where it is in the files), never mistaken for a YouTube
    id. Its end counts too: cut differently (from the epub), it's another episode."""
    where = f"{ch.part}#{ch.start:.3f}-{ch.end:.3f}" + "".join(f"+{p}#{s:.3f}-{e:.3f}" for p, s, e in ch.more)
    return "book" + hashlib.sha256(where.encode()).hexdigest()[:16]


def _item_url(ch: Chapter) -> str:
    query = {"n": ch.number, "t": f"{ch.start:.3f},{ch.end:.3f}"}
    if ch.more:
        query["more"] = json.dumps([[p, round(s, 3), round(e, 3)] for p, s, e in ch.more])
    return PREFIX + urllib.parse.quote(ch.part) + "#" + urllib.parse.urlencode(query)


def item_part(url: str) -> str:
    """The file (relative to the books folder) a chapter item begins in."""
    return urllib.parse.unquote(url.removeprefix(PREFIX).partition("#")[0])


def fetch(source: str) -> Feed:
    """The book as a show listing, newest (last chapter) first like any feed."""
    book = read(source)
    thumb = cover(source)
    items = [FeedItem(item_id=item_id(ch), url=_item_url(ch), title=ch.title, duration=round(ch.duration),
                      live_status=None, thumb_url=f"/media/{thumb}" if thumb else None)
             for ch in reversed(book["chapters"])]
    return Feed(title=book["title"], items=items, author=book["author"])


# ---- chapters from the epub -------------------------------------------------------------

PLACEMENT_DIR = DATA_DIR / "chapters"
CHAPTER_WORDS = 100  # an epub entry this long is a chapter (shorter: a part's title page, a dedication; a prologue can be under 300)
MIN_PLACED = 0.5  # if fewer of the chapters than this are found, the files' own marks are used instead

current: dict | None = None  # the book being placed, for the app: {"source", "step"}


def _key(text: Path, parts: list[Path]) -> str:
    """Changes when the epub or any audio file does, so the chapters are placed again."""
    return json.dumps([[text.name, text.stat().st_size]] + [[p.name, p.stat().st_size] for p in parts])


def _placement_path(source: str) -> Path:
    return PLACEMENT_DIR / f"{hashlib.sha256(source.encode()).hexdigest()[:16]}.json"


def _load_placement(source: str, key: str) -> dict | None:
    try:
        placed = json.loads(_placement_path(source).read_text())
    except (OSError, ValueError):
        return None
    return placed if placed.get("key") == key else None


def needs_placing(source: str) -> bool:
    """Whether the book has an epub whose chapters haven't been found in its audio
    yet. (Cheap: asked on every refresh of the app.)"""
    try:
        path = _resolve(source.removeprefix(PREFIX))
        parts, text = _parts(path), _epub_for(path)
    except (OSError, ValueError):
        return False
    return bool(parts and text and transcribe.available()) and _load_placement(source, _key(text, parts)) is None


def transcript_id(part: Path) -> str:
    return "book" + hashlib.sha256(f"{part.relative_to(BOOKS_DIR.resolve())}:{part.stat().st_size}".encode()).hexdigest()[:16]


def place_chapters(source: str) -> dict:
    """Transcribe the book's audio and find its epub's chapters in it; saved, so
    read() uses them from now on. Transcripts are kept a while (like episodes'),
    so trying again doesn't mean transcribing again."""
    global current
    path = _resolve(source.removeprefix(PREFIX))
    parts = _parts(path)
    text = _epub_for(path)
    info = epub.read(text)
    root = BOOKS_DIR.resolve()
    transcripts = []
    try:
        for n, part in enumerate(parts, 1):
            current = {"source": source, "step": f"transcribing part {n} of {len(parts)}" if len(parts) > 1
                       else "transcribing"}
            t = transcribe.load(transcript_id(part))
            if t is None:
                t = transcribe.transcribe(part)
                transcribe.save(transcript_id(part), t)
            transcripts.append(t)
        current = {"source": source, "step": "finding chapters"}
        placed = epub.place(info["entries"], transcripts)
    finally:
        current = None
    lengths = [float(_probe(p)["format"]["duration"]) for p in parts]
    result = _chapters(info["entries"], placed, [p.relative_to(root).as_posix() for p in parts], lengths)
    wanted = sum(len(e.words) >= CHAPTER_WORDS for e in info["entries"])
    found = sum(p is not None and len(e.words) >= CHAPTER_WORDS for e, p in zip(info["entries"], placed))
    if not wanted or found < MIN_PLACED * wanted:
        result = {"chapters": [], "note": f"only {found} of {wanted} chapters found in the audio"}
    result["key"] = _key(text, parts)
    PLACEMENT_DIR.mkdir(parents=True, exist_ok=True)
    _placement_path(source).write_text(json.dumps(result))
    log.info("%s: %s", source, result.get("note") or f"{len(result['chapters'])} chapters placed from {text.name}")
    return result


def _chapters(entries, placed, parts: list[str], lengths: list[float]) -> dict:
    """Episodes from the placed entries: each runs to the next one, on into the
    next file where it crosses one. A part's title page (short, but spoken)
    begins the chapter after it; a chapter that wasn't found stays in the one
    before (unless nothing after it was found either: back matter, not read);
    whatever comes before the first is part of the first (the credits)."""
    starts, unplaced, heading, missing = [], [], None, []
    for e, p in zip(entries, placed):
        if p is None:
            if len(e.words) >= CHAPTER_WORDS:
                missing.append(e.title)  # in the chapter before, if a later one is found (else back matter)
                unplaced.append(e.title)
            continue
        _, f, t, _ = p
        if len(e.words) < CHAPTER_WORDS:
            heading = heading or {"file": f, "time": t, "title": e.title}
            continue
        if starts and missing:
            starts[-1]["title"] += "".join(f" & {m}" for m in missing)
        missing = []
        start = heading or {"file": f, "time": t}
        starts.append({"file": start["file"], "time": start["time"],
                       "title": f"{heading['title']} · {e.title}" if heading else e.title})
        heading = None
    if starts:
        starts[0].update(file=0, time=0.0)
    chapters = []
    for a, b in zip(starts, starts[1:] + [{"file": len(parts) - 1, "time": lengths[-1]}]):
        pieces = [[parts[a["file"]], a["time"], lengths[a["file"]] if b["file"] != a["file"] else b["time"]]]
        for f in range(a["file"] + 1, b["file"] + 1):
            pieces.append([parts[f], 0.0, b["time"] if f == b["file"] else lengths[f]])
        pieces = [p for p in pieces if p[2] - p[1] >= 0.5]
        chapters.append({"title": a["title"], "pieces": pieces})
    return {"chapters": chapters, "unplaced": unplaced}


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
    """Cut one chapter out of its part (no re-encoding) and add it to the library.
    One that goes on into the next file is joined from its pieces, still losslessly."""
    part, _, fragment = item["url"].removeprefix(PREFIX).partition("#")
    q = urllib.parse.parse_qs(fragment)
    number = int(q["n"][0])
    start, end = (float(x) for x in q["t"][0].split(","))
    pieces = [(urllib.parse.unquote(part), start, end)] + [tuple(p) for p in json.loads(q.get("more", ["[]"])[0])]
    files = [_resolve(p) for p, _, _ in pieces]
    if not all(f.is_file() for f in files):
        raise RuntimeError("the book's file is gone from the books folder")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = MEDIA_DIR / f"{item['item_id']}{'.mp3' if files[0].suffix.lower() == '.mp3' else '.m4a'}"
    tmp = path.with_name(path.stem + ".part" + path.suffix)
    out = ["-map", "0:a:0", "-c", "copy", "-map_chapters", "-1", "-metadata", f"title={item['title']}",
           "-movflags", "+faststart", str(tmp)]
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as listing:
        if len(pieces) == 1:
            cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(files[0]), "-t", f"{end - start:.3f}"]
        else:
            for f, (_, s, e) in zip(files, pieces):
                quoted = str(f).replace("'", "'\\''")
                listing.write(f"file '{quoted}'\ninpoint {s:.3f}\noutpoint {e:.3f}\n")
            listing.flush()
            cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", listing.name]
        run = subprocess.run(cmd + out, capture_output=True, text=True)
    if run.returncode:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"couldn't cut the chapter out: {run.stderr.strip()[:200]}")
    tmp.replace(path)
    book = show["name"]
    episode = {
        "video_id": item["item_id"], "mode": "audio", "title": item["title"],
        "channel": show.get("author"), "duration": round(sum(e - s for _, s, e in pieces)),
        # In chapter order, a second apart: podcast apps order a serial feed by date.
        "upload_date": None, "published_at": show["created_at"] + number,
        "description": f"{book}, chapter {number}.", "source_url": item["url"],
        "file": path.relative_to(MEDIA_DIR).as_posix(), "thumb": cover(show["feed_url"]),
        "show_id": show["id"], "number": number,
    }
    db.upsert_episode(episode)
    return episode
