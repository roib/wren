"""Chapters for episodes whose ads are being removed: from the transcript Wren
already made, written into the audio file in the same once-only step as the cuts.

Tried on five episodes before building this: where the creator published
chapters, Claude found the same topic changes within seconds. A creator's own
list can be wrong, though (one show's description carried another episode's
chapters), so it's used only when it fits the episode and agrees with
Claude's; otherwise Claude's are. Asked for one every 5 minutes, long episodes
got near-duplicates, hence 8 to 15 minutes and merging.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel

from . import ads, notes

RULES = """You divide a podcast episode into chapters, like the chapter list a good podcast
publishes. Each line of the transcript is [start-end seconds] text.

A chapter is one topic, story or segment. Start a new chapter where the conversation clearly moves to
something else, not at every passing tangent. Aim for about one chapter every 8 to 15 minutes (a news
show's separate stories can be shorter, down to about 3 minutes), and merge neighbouring stretches on the
same subject into one chapter. Never under 2 minutes, except a short opening. The first chapter starts at
the first line. Ad reads and promos are not chapters: don't start a chapter at one; the chapter after an
ad starts where the conversation picks up again.

For each chapter give "start" = the start time of the line where it begins, "first_words" = its first
five words exactly as in the transcript, and "title": short (2 to 7 words), specific and plain, the way a
podcast titles its chapters ("Growing up in Louisiana", "Why the party wants to leave the EU"). No
numbering, no "Chapter 1", no clickbait."""


class Chapter(BaseModel):
    start: float
    first_words: str = ""
    title: str


class Chapters(BaseModel):
    chapters: list[Chapter]


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["chapters"],
          "properties": {"chapters": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["start", "first_words", "title"],
              "properties": {"start": {"type": "number"}, "first_words": {"type": "string"},
                             "title": {"type": "string"}}}}}}

MIN_GAP = 120  # seconds: a chapter closer than this to the one before is dropped
FIND_AHEAD = 60  # how far after the given time to look for a chapter's first words
AGREE_WITHIN = 90  # a creator's chapter agrees with Claude's if one starts this close
AGREE_SHARE = 0.5  # and at least this share of them must


def request_id(video_id: str) -> str:
    return f"{video_id}-chapters"


def ask(transcript: dict, title: str, video_id: str) -> Chapters:
    """Claude's chapters. Raises ads.Pending while the host helper is on it."""
    return ads.ask(request_id(video_id), RULES, SCHEMA, Chapters,
                   f"Episode: {title}\n\nTRANSCRIPT\n{ads.transcript_text(transcript)}")


def place(answer: Chapters, transcript: dict) -> list[dict]:
    """[{start, title}] on the original timeline: each chapter at its first words
    (the first three if all five aren't found), in the pause before them."""
    spoken, pauses = ads._spoken(transcript), ads._pauses(transcript)
    out = []
    for ch in sorted(answer.chapters, key=lambda c: c.start):
        at = ch.start
        words = ch.first_words.split()
        for phrase in (words, words[:3]):
            if hits := ads._find(spoken, " ".join(phrase), ch.start - ads.SNAP_WINDOW, ch.start + FIND_AHEAD):
                at = ads.snap_start(hits[0][0], pauses)
                break
        title = " ".join(ch.title.split())
        if not title:
            continue
        if not out:
            at = 0.0  # whatever comes before the first chapter (a cold open, credits) belongs to it
        elif at - out[-1]["start"] < MIN_GAP:
            continue
        out.append({"start": round(at, 2), "title": title})
    return out


_LINE = re.compile(r"^\W*((?:(\d{1,2}):)?(\d{1,3}):(\d{2}))\b\s*[-–—:|·.)]*\s*(.+)$")


def from_description(description: str | None, duration: float | None) -> list[dict]:
    """The creator's chapter list in the description ("00:00 Intro", "12:34 - Topic"),
    if it fits the episode: starts at the beginning, in order, ending before the end."""
    out = []
    for line in notes.lines(description):
        if m := _LINE.match(line):
            t = int(m[2] or 0) * 3600 + int(m[3]) * 60 + int(m[4])
            out.append({"start": float(t), "title": m[5].strip()})
    if len(out) < 2 or out[0]["start"] > 5:
        return []
    if any(b["start"] <= a["start"] for a, b in zip(out, out[1:])):
        return []
    if duration and out[-1]["start"] >= duration:
        return []
    out[0]["start"] = 0.0
    return out


def agrees(creator: list[dict], generated: list[dict]) -> bool:
    """Whether most of the creator's chapter starts (after the first) match one of Claude's."""
    later = creator[1:]
    if not later or not generated:
        return False
    close = sum(any(abs(c["start"] - g["start"]) <= AGREE_WITHIN for g in generated) for c in later)
    return close >= AGREE_SHARE * len(later)


def choose(creator: list[dict], generated: list[dict]) -> tuple[list[dict], str | None]:
    """The chapters to use and where they came from ("creator" or "transcript")."""
    if creator and agrees(creator, generated):
        return creator, "creator"
    if generated:
        return generated, "transcript"
    return [], None


def after_cuts(chapters: list[dict], segments: list[tuple[float, float]]) -> list[dict]:
    """The chapters on the cut audio's timeline: moved back by the audio cut before
    them (one that started in an ad starts where the ad was). Chapters that end
    up at the same time keep the later one, which is where the talk resumes."""
    def moved(t: float) -> float:
        return t - sum(min(end, t) - start for start, end in segments if start < t)

    out: list[dict] = []
    for ch in chapters:
        at = round(moved(ch["start"]), 2)
        if out and at - out[-1]["start"] < 1:
            out.pop()
        out.append({**ch, "start": at})
    if out:
        out[0]["start"] = 0.0
    return out


def write(path: Path, chapters: list[dict], duration: float) -> None:
    """Put the chapters into the media file itself (MP4 chapter track, or ID3
    chapters in an MP3), copying the streams. That's what Apple Podcasts reads."""
    if not chapters or path.suffix not in (".m4a", ".mp4", ".mp3"):
        return
    ends = [c["start"] for c in chapters[1:]] + [duration]
    meta = [";FFMETADATA1"]
    for ch, end in zip(chapters, ends):
        title = re.sub(r"([=;#\\\n])", r"\\\1", ch["title"])
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(ch['start'] * 1000)}",
                 f"END={int(max(end, ch['start'] + 1) * 1000)}", f"title={title}"]
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        listing = Path(tmp) / "chapters.txt"
        listing.write_text("\n".join(meta) + "\n")
        out = Path(tmp) / f"chaptered{path.suffix}"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-f", "ffmetadata", "-i", str(listing),
                        "-map", "0", "-map_metadata", "0", "-map_chapters", "1", "-c", "copy",
                        *(["-movflags", "+faststart"] if path.suffix in (".m4a", ".mp4") else ["-id3v2_version", "3"]),
                        str(out)], check=True)
        out.replace(path)


def for_episode(ep: dict) -> list[dict]:
    """An episode's chapters as listeners get them: on the cut audio's timeline."""
    decision = json.loads(ep.get("sponsor_cuts") or "null") or {}
    segments = [tuple(s) for s in json.loads(ep.get("sponsor_segments") or "[]")]
    return after_cuts(decision.get("chapters") or [], segments)


def json_chapters(ep: dict) -> dict:
    """The Podcasting 2.0 chapters file (for apps that read the feed's podcast:chapters)."""
    return {"version": "1.2.0", "chapters": [{"startTime": c["start"], "title": c["title"]} for c in for_episode(ep)]}
