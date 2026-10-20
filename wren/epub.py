"""A book's text (a DRM-free .epub beside the audio), for finding its chapters in the audio.

Audiobooks often have no useful chapter marks, and dramatized ones don't read
chapter titles aloud. The epub has each chapter's own words, though: its
opening is looked for in a Whisper transcript (three-word runs, in book order,
so chapter 9 can only come after chapter 8). Tested on a full-cast
dramatization: every chapter placed, each cut right before its first words.
"""

import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

NS = {"opf": "http://www.idpf.org/2007/opf", "ncx": "http://www.daisy.org/z3986/2005/ncx/",
      "c": "urn:oasis:names:tc:opendocument:xmlns:container", "dc": "http://purl.org/dc/elements/1.1/",
      "x": "http://www.w3.org/1999/xhtml", "epub": "http://www.idpf.org/2007/ops"}


@dataclass
class Entry:
    title: str
    words: list[str]  # the entry's text as normalised words, its headings left out


class _Body(HTMLParser):
    """A page's text without its headings: a chapter's title usually isn't read aloud."""
    SKIP = {"h1", "h2", "h3", "h4", "h5", "h6", "script", "style", "head", "title"}

    def __init__(self):
        super().__init__()
        self.depth, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("’", "").replace("'", ""))


def _toc(z: zipfile.ZipFile, opf_path: str, opf) -> list[tuple[str, str]]:
    """[(title, page)] in reading order, from the NCX (EPUB 2) or the nav page (EPUB 3)."""
    base = posixpath.dirname(opf_path)
    items = list(opf.findall(".//opf:manifest/opf:item", NS))

    def resolve(ref: str, rel_to: str) -> str:
        return posixpath.normpath(posixpath.join(posixpath.dirname(rel_to), urllib.parse.unquote(ref.split("#")[0])))

    ncx = next((i for i in items if i.get("media-type") == "application/x-dtbncx+xml"), None)
    if ncx is not None:
        path = posixpath.join(base, ncx.get("href"))
        return [(" ".join("".join(p.find("ncx:navLabel/ncx:text", NS).itertext()).split()),
                 resolve(p.find("ncx:content", NS).get("src"), path))
                for p in ET.fromstring(z.read(path)).iter(f"{{{NS['ncx']}}}navPoint")]
    nav = next((i for i in items if "nav" in (i.get("properties") or "").split()), None)
    if nav is not None:
        path = posixpath.join(base, nav.get("href"))
        root = ET.fromstring(z.read(path))
        toc = next((n for n in root.iter(f"{{{NS['x']}}}nav") if n.get(f"{{{NS['epub']}}}type") == "toc"), None)
        if toc is not None:
            return [(" ".join("".join(a.itertext()).split()), resolve(a.get("href"), path))
                    for a in toc.iter(f"{{{NS['x']}}}a") if a.get("href")]
    raise ValueError("the epub has no table of contents")


def read(path: Path) -> dict:
    """The book's title, author and table of contents with each entry's words."""
    with zipfile.ZipFile(path) as z:
        if "META-INF/encryption.xml" in z.namelist():
            raise ValueError("the epub is copy-protected (DRM)")
        opf_path = ET.fromstring(z.read("META-INF/container.xml")).find(".//c:rootfile", NS).get("full-path")
        opf = ET.fromstring(z.read(opf_path))
        entries, seen = [], set()
        for title, page in _toc(z, opf_path, opf):
            if page in seen or page not in z.namelist():
                continue  # several entries in one page: the page's first one
            seen.add(page)
            body = _Body()
            body.feed(z.read(page).decode("utf-8", "replace"))
            entries.append(Entry(title, words(" ".join(body.parts))))
    return {"title": (opf.findtext(".//dc:title", namespaces=NS) or "").strip() or None,
            "author": (opf.findtext(".//dc:creator", namespaces=NS) or "").strip() or None,
            "entries": entries}


# ---- placing entries in the audio ---------------------------------------------------

N = 3  # word runs matched
OPENING = 80  # words of each entry's opening looked for
MIN_WORDS = 10  # entries shorter than this (a cover, a title page) aren't looked for
PAUSE = 1.0  # seconds: a gap this long between words is a pause


class _Transcript:
    def __init__(self, transcript: dict):
        self.words = [(w[0], w[1], x) for s in transcript["segments"] for w in s.get("words") or []
                      for x in words(w[2])]
        self.index = defaultdict(list)
        for i in range(len(self.words) - N + 1):
            self.index[tuple(w[2] for w in self.words[i:i + N])].append(i)

    def gap_before(self, i: int) -> float:
        return self.words[i][0] - (self.words[i - 1][1] if i else 0.0)

    def find(self, opening: list[str], after: int) -> tuple[int, int] | None:
        """Where an entry with this opening begins (word index), and how many of
        its word runs agree, if enough do."""
        starts = []  # (where the entry would begin, offset of the run in the opening)
        for k in range(len(opening) - N + 1):
            starts += [(i - k, k) for i in self.index.get(tuple(opening[k:k + N]), ()) if i >= after]
        if not starts:
            return None
        # The densest cluster of implied beginnings, within ~25 words of each other.
        counts = Counter(st // 5 for st, _ in starts)
        best = max(counts, key=lambda b: (sum(counts.get(b + d, 0) for d in range(-5, 6)), -b))
        cluster = [(st, k) for st, k in starts if abs(st // 5 - best) <= 5]
        if len(cluster) < max(5, (len(opening) - N + 1) // 8):
            return None
        # Its first word: implied by the earliest part of the opening that matched.
        st = max(min(cluster, key=lambda c: (c[1], c[0]))[0], after)
        # With no pause there, the first word may have run into the line before
        # (Whisper's doing): step back to a pause a word or two earlier.
        if self.gap_before(st) < PAUSE:
            for j in range(st - 1, max(after, st - 4), -1):
                if self.gap_before(j) >= PAUSE:
                    st = j
                    break
        return st, len(cluster)

    def cut_at(self, i: int) -> float:
        """Where to cut before word i: in the pause before it, close to the word."""
        return 0.0 if i == 0 else max(self.words[i - 1][1], self.words[i][0] - 1.5)


def place(entries: list[Entry], transcripts: list[dict]) -> list[tuple[int, int, float, int] | None]:
    """For each entry: (its index, the audio file it begins in, the time, the match
    score), or None where it couldn't be placed. Entries are looked for in order:
    in the current file after the previous one, else in a later file."""
    files = [_Transcript(t) for t in transcripts]
    out, f, pos = [], 0, 0
    for n, e in enumerate(entries):
        found = None
        if len(e.words) >= MIN_WORDS:
            opening = e.words[:OPENING]
            for g in range(f, len(files)):
                hit = files[g].find(opening, pos if g == f else 0)
                if hit:
                    found = (g, *hit)
                    break
        if found is None:
            out.append(None)
            continue
        f, st, score = found
        pos = st + 1
        out.append((n, f, files[f].cut_at(st), score))
    return out
