"""Chapters from transcripts: placing them, the creator's own list, the cut timeline, the file."""

import json
import subprocess

from wren import chapters
from wren.chapters import Chapter, Chapters


def words(text, start=0.0, gap_after=()):
    """One word a second; a 2-second pause before each word index in gap_after."""
    out, t = [], start
    for i, w in enumerate(text.split()):
        if i in gap_after:
            t += 2
        out.append([t, t + 0.8, f" {w}"])
        t += 1
    return {"segments": [{"start": start, "end": t, "text": text, "words": out}], "seconds": 1.0}


TALK = words(" ".join(f"x{i}" for i in range(400)), gap_after={150, 300})


def test_chapters_start_at_their_first_words():
    answer = Chapters(chapters=[
        Chapter(start=5, first_words="x0 x1 x2", title="Opening"),
        Chapter(start=140, first_words="x150 x151 x152 x153 x154", title="  The   middle "),
        Chapter(start=200, first_words="x200", title="Too soon after"),          # under 2 minutes after the last
        Chapter(start=300, first_words="x300 x301 x302 not-said words", title="The end"),  # first three words found
    ])
    placed = chapters.place(answer, TALK)
    # Each in the pause before its first word; the first at the very start.
    assert [(round(c["start"]), c["title"]) for c in placed] == [(0, "Opening"), (151, "The middle"), (303, "The end")]


def test_creator_chapters_must_fit_the_episode():
    desc = "About it.\n00:00 Intro\n03:00 - The middle\n1:02:03 — The end"
    assert chapters.from_description(desc, 4000) == [
        {"start": 0.0, "title": "Intro"}, {"start": 180.0, "title": "The middle"}, {"start": 3723.0, "title": "The end"}]
    assert chapters.from_description(desc, 3000) == []                   # runs past the end: another episode's
    assert chapters.from_description("01:00 A\n03:00 B", 4000) == []     # doesn't start at the beginning
    assert chapters.from_description("00:00 A\n03:00 B\n02:00 C", 4000) == []  # out of order
    assert chapters.from_description("Just text", 4000) == []


def test_creator_chapters_are_used_only_when_they_agree():
    creator = [{"start": 0.0, "title": "Intro"}, {"start": 600.0, "title": "B"}, {"start": 1200.0, "title": "C"}]
    close = [{"start": 0.0, "title": "x"}, {"start": 650.0, "title": "y"}, {"start": 1500.0, "title": "z"}]
    far = [{"start": 0.0, "title": "x"}, {"start": 900.0, "title": "y"}]
    assert chapters.choose(creator, close) == (creator, "creator")
    assert chapters.choose(creator, far) == (far, "transcript")
    assert chapters.choose([], far) == (far, "transcript")
    assert chapters.choose([], []) == ([], None)


def test_chapter_times_follow_the_cuts():
    chs = [{"start": 0.0, "title": "A"}, {"start": 100.0, "title": "B"}, {"start": 130.0, "title": "C"},
           {"start": 300.0, "title": "D"}]
    # A 60s ad from 90s: B started inside it and C right after it, both where the ad was: C stays.
    moved = chapters.after_cuts(chs, [(90.0, 150.0)])
    assert moved == [{"start": 0.0, "title": "A"}, {"start": 90.0, "title": "C"}, {"start": 240.0, "title": "D"}]
    assert chapters.after_cuts(chs, []) == chs


def test_chapters_are_written_into_the_file(tmp_path):
    for name in ("a.m4a", "a.mp3"):
        path = tmp_path / name
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=6", str(path)], check=True)
        chapters.write(path, [{"start": 0.0, "title": "Intro"}, {"start": 3.0, "title": "Talk; = #2"}], 6)
        out = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(path)],
                                        capture_output=True, text=True, check=True).stdout)
        assert [(round(float(c["start_time"])), c["tags"]["title"]) for c in out["chapters"]] == [
            (0, "Intro"), (3, "Talk; = #2")]
