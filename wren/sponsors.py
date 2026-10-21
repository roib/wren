"""Cut sponsor reads out of episodes, using SponsorBlock's community marks.

Episodes of shows with skip_sponsors on are downloaded as usual but held back
from the feeds ("pending") until SPONSOR_DELAY_HOURS after YouTube published
them, by when the marks are in. Then each is processed exactly once: the marked
sponsor segments are cut out (losslessly, no re-encoding) and it joins the feed.
The file isn't changed after that, so a podcast app never holds a stale copy.

With Claude and Whisper available ("transcript" mode), there's no wait: an
episode is processed right after downloading, cutting both the ad reads Claude
finds in a local transcript (ads.py, transcribe.py) and any SponsorBlock marks.
Both, because so soon after publishing SponsorBlock may have only some of an
episode's ads marked.

SponsorBlock (sponsor.ajay.app) is run by volunteers; lookups send only the
first characters of a hash of the video id. Its data is CC BY-NC-SA 4.0.
"""

import hashlib
import json
import logging
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import ads, chapters, db, notes, transcribe
from .config import MEDIA_DIR, SPONSOR_DELAY_HOURS

log = logging.getLogger("wren")

API = "https://sponsor.ajay.app/api/skipSegments/"
CATEGORIES = ["sponsor"]
USER_AGENT = "wren (self-hosted podcast downloader)"
MIN_SEGMENT = 1.0  # seconds; shorter marks aren't worth a cut
# If SponsorBlock can't be reached, keep trying this long past the delay, then
# publish the episode uncut rather than hold it forever.
GIVE_UP_SECONDS = 12 * 3600
RETRY_SECONDS = 15 * 60
ANSWER_POLL_SECONDS = 60  # how often to look for the host helper's answer

_next_try: dict[tuple[str, str], float] = {}  # (video_id, mode) -> after a failed attempt

# What's being processed right now, for the app: {"title", "step"}.
current: dict | None = None


def mode() -> str:
    """"transcript" when ad reads can be found without SponsorBlock, else "sponsorblock"."""
    return "transcript" if ads.available() and transcribe.available() else "sponsorblock"


def ready_at(ep: dict) -> int:
    """When a held-back episode is due. Waiting for SponsorBlock: the delay
    after YouTube published it (or after the download, if that isn't known).
    With transcripts: straight away."""
    if mode() == "transcript":
        return int(ep["added_at"])
    return int((ep.get("published_at") or ep["added_at"]) + SPONSOR_DELAY_HOURS * 3600)


def lookup(video_id: str) -> list[tuple[float, float]]:
    """The video's sponsor segments as (start, end) seconds. [] if none are
    marked; raises if SponsorBlock can't be reached."""
    return [(a, b) for a, b, _ in lookup_categories(video_id, CATEGORIES)]


def lookup_categories(video_id: str, categories: list[str]) -> list[tuple[float, float, str]]:
    """The video's segments in the given SponsorBlock categories, as (start, end, category)."""
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    url = API + prefix + "?" + urllib.parse.urlencode({"categories": json.dumps(categories)})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            videos = json.load(res)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # nothing marked for any video with this prefix
            return []
        raise
    video = next((v for v in videos if v.get("videoID") == video_id), None)
    return [(*s["segment"], s.get("category", "sponsor")) for s in (video or {}).get("segments", [])
            if s.get("actionType", "skip") == "skip"]


EXTRA_CATEGORIES = ["intro", "outro"]  # for shows that also cut their intro and outro


def _intro_marks(ep: dict) -> list[dict]:
    """The intro and outro marked on SponsorBlock, for shows set to cut them."""
    with db.connect() as conn:
        row = conn.execute("SELECT skip_intros, kind FROM shows WHERE id = ?", (ep.get("show_id"),)).fetchone()
    if not row or not row["skip_intros"] or row["kind"] != "youtube":
        return []
    out = []
    for category in EXTRA_CATEGORIES:
        spans = [(a, b) for a, b, c in lookup_categories(ep["video_id"], [category]) if c == category]
        out += [{"start": round(a, 1), "end": round(b, 1), "source": "sponsorblock", "category": category}
                for a, b in merge(spans, ep.get("duration"))]
    return out


def merge(segments: list[tuple[float, float]], duration: float | None) -> list[tuple[float, float]]:
    """Overlapping or touching marks joined, clamped to the episode, tiny ones dropped."""
    out: list[list[float]] = []
    for start, end in sorted(segments):
        start = max(0.0, start)
        if duration:
            end = min(end, float(duration))
        if end - start < MIN_SEGMENT:
            continue
        if out and start <= out[-1][1] + 0.5:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(a, b) for a, b in out]


def cut(path: Path, segments: list[tuple[float, float]]) -> None:
    """Remove the segments from a media file in place, copying the streams (no
    re-encoding). Audio cuts land within a frame; video ones at keyframes."""
    keep, pos = [], 0.0
    for start, end in segments:
        if start > pos:
            keep.append((pos, start))
        pos = end
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        listing = Path(tmp) / "parts.ffconcat"
        lines = ["ffconcat version 1.0"]
        for start, end in keep + [(pos, None)]:
            lines.append(f"file '{path.resolve()}'")
            lines.append(f"inpoint {start:.3f}")
            if end is not None:
                lines.append(f"outpoint {end:.3f}")
        listing.write_text("\n".join(lines) + "\n")
        out = Path(tmp) / f"cut{path.suffix}"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                        "-map", "0", "-c", "copy", "-movflags", "+faststart", str(out)], check=True)
        out.replace(path)


def _step(ep: dict, step: str) -> None:
    global current
    current = {"title": ep["title"], "step": step}


def _other_copy_cuts(ep: dict) -> dict | None:
    """The cuts already decided for this video's other copy (audio/video share a timeline)."""
    with db.connect() as conn:
        row = conn.execute("""SELECT sponsor_cuts FROM episodes WHERE video_id = ? AND mode != ?
                              AND sponsor_state = 'done' AND sponsor_cuts IS NOT NULL""",
                           (ep["video_id"], ep["mode"])).fetchone()
    return json.loads(row["sponsor_cuts"]) if row else None


def _is_podcast(ep: dict) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT kind FROM shows WHERE id = ?", (ep.get("show_id"),)).fetchone()
    return bool(row and row["kind"] == "rss")


def decide(ep: dict) -> dict:
    """What to cut from an episode: {"cuts": [{start, end, source, ...}], "note": why nothing was, or None}."""
    reused = _other_copy_cuts(ep)
    if reused is not None:
        return reused
    _step(ep, "checking SponsorBlock")
    try:
        # SponsorBlock only knows YouTube videos; podcast-feed episodes rely on the transcript.
        marked = [] if _is_podcast(ep) else merge(lookup(ep["video_id"]), ep.get("duration"))
    except Exception:
        if mode() != "transcript":
            raise  # nothing else to go on: try again later
        log.warning("SponsorBlock unreachable; using the transcript for %s", ep["title"])
        marked = []
    marks = [{"start": round(a, 1), "end": round(b, 1), "source": "sponsorblock"} for a, b in marked]
    try:
        extras = _intro_marks(ep)
    except Exception:
        extras = []  # SponsorBlock trouble: keep the intro rather than hold the episode
    if mode() != "transcript":
        return {"cuts": sorted(marks + extras, key=lambda c: c["start"]), "note": None}
    transcript = transcribe.load(ep["video_id"])
    if transcript is None:
        _step(ep, "transcribing")
        transcript = transcribe.transcribe(MEDIA_DIR / ep["file"])
        transcribe.save(ep["video_id"], transcript)
        log.info("Transcribed %s in %ss", ep["title"], transcript["seconds"])
    _step(ep, "waiting for Claude" if ads.backend() == "claude-cli" else "finding ad reads")
    # Both questions go out together; whichever isn't answered yet is waited for.
    pending, answer, chaptered = None, None, None
    try:
        answer = ads.ask_claude(transcript, ep["title"], ep["video_id"], ep.get("description"))
    except ads.Pending as e:
        pending = e
    try:
        chaptered = chapters.ask(transcript, ep["title"], ep["video_id"])
    except ads.Pending as e:
        pending = e
    except Exception as e:
        log.warning("%s: no chapters: %s", ep["title"], e)  # the episode goes out without them
    if pending:
        raise pending
    found, note = ads.choose(answer.ads, transcript, ep.get("duration"))
    if note:
        log.warning("%s: %s", ep["title"], note)
    decision = {"cuts": sorted(combine(marks, found) + extras, key=lambda c: c["start"]), "note": note}
    if answer.description_lines:
        decision["description_lines"] = sorted(set(answer.description_lines))
    # A podcast's show-note times are off by the ads its host inserted: only YouTube's are tried.
    creator = [] if _is_podcast(ep) else chapters.from_description(ep.get("description"), ep.get("duration"))
    chosen, source = chapters.choose(creator, chapters.place(chaptered, transcript) if chaptered else [])
    if chosen:
        decision["chapters"], decision["chapters_source"] = chosen, source  # on the original timeline
    return decision


def combine(marks: list[dict], found: list[dict]) -> list[dict]:
    """SponsorBlock's marks plus the ad reads found in the transcript. Where a
    find overlaps a mark, the mark's boundaries stand (people adjusted them) and
    it takes the advertiser's name; finds that overlap no mark are cut too."""
    out = [dict(m) for m in marks]
    for c in found:
        hit = [m for m in out if m["source"] == "sponsorblock"
               and min(m["end"], c["end"]) - max(m["start"], c["start"]) > 0]
        for m in hit:
            names = [n for n in (m.get("advertiser"), c.get("advertiser")) if n]
            m["advertiser"] = ", ".join(dict.fromkeys(names)) or None
        if not hit:
            out.append(c)
    for m in out:
        if m.get("advertiser") is None:
            m.pop("advertiser", None)
    return sorted(out, key=lambda c: c["start"])


def _description(ep: dict, decision: dict, segments: list[tuple[float, float]]) -> str | None:
    """The episode's description without the lines Claude marked as ads (kept in
    the decision, for the app), and for YouTube, whose chapter times are the
    video's, those times moved to match the cut audio."""
    description = ep.get("description")
    if not description:
        return description
    if decision.get("description_lines"):
        description, decision["description_removed"] = notes.remove(description, set(decision["description_lines"]))
    if not _is_podcast(ep):
        # A podcast's own times are already off by whatever ads its host inserted.
        description = notes.retime(description, segments)
    return description


def _forget(ep: dict) -> None:
    """The episode is done with: its questions to Claude go."""
    ads.forget(ep["video_id"])
    ads.forget(chapters.request_id(ep["video_id"]))


def process(ep: dict) -> None:
    """Decide and make the cuts in one held-back episode, then let it into the feeds."""
    if not (MEDIA_DIR / ep["file"]).exists():
        raise FileNotFoundError(ep["file"])
    decision = decide(ep)
    segments = merge([(c["start"], c["end"]) for c in decision["cuts"]], ep.get("duration"))
    if segments:
        _step(ep, "cutting")
        cut(MEDIA_DIR / ep["file"], segments)
    removed = round(sum(end - start for start, end in segments))
    if decision.get("chapters"):
        _step(ep, "adding chapters")
        marks = chapters.after_cuts(decision["chapters"], segments)
        chapters.write(MEDIA_DIR / ep["file"], marks, (ep.get("duration") or marks[-1]["start"] + 60) - removed)
    description = _description(ep, decision, segments)
    with db.connect() as conn:
        conn.execute(
            """UPDATE episodes SET sponsor_state = 'done', sponsor_removed = ?, sponsor_segments = ?, sponsor_cuts = ?,
                                   duration = CASE WHEN duration IS NULL THEN NULL ELSE MAX(0, duration - ?) END,
                                   description = ?
               WHERE video_id = ? AND mode = ?""",
            (removed, json.dumps([[round(a, 1), round(b, 1)] for a, b in segments]), json.dumps(decision), removed,
             description, ep["video_id"], ep["mode"]))
    _forget(ep)
    sources = sorted({c["source"] for c in decision["cuts"]})
    if segments:
        log.info("Removed %d sponsor read(s), %ds, from %s (%s; %s)", len(segments), removed, ep["title"], ep["mode"],
                 ", ".join(sources))
    else:
        log.info("No sponsor reads cut from %s (%s)", ep["title"], ep["mode"])


def process_next(now: float | None = None) -> bool:
    """Process the oldest held-back episode that's due. Returns False when none is."""
    global current
    try:
        return _process_next(now or time.time())
    finally:
        current = None


def _process_next(now: float) -> bool:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM episodes WHERE sponsor_state = 'pending' ORDER BY added_at")]
    for ep in rows:
        key = (ep["video_id"], ep["mode"])
        if ready_at(ep) > now or _next_try.get(key, 0) > now:
            continue
        try:
            process(ep)
        except FileNotFoundError:
            # Deleted while it was being processed (by hand, or a keep-last limit).
            log.info("%s was removed before its sponsor reads were cut", ep["title"])
        except ads.Pending:
            if now < ready_at(ep) + GIVE_UP_SECONDS:
                _next_try[key] = now + ANSWER_POLL_SECONDS  # asked; look again shortly
                return True
            log.warning("Publishing %s uncut: no answer from Claude on the server", ep["title"])
            with db.connect() as conn:
                conn.execute("UPDATE episodes SET sponsor_state = 'done' WHERE video_id = ? AND mode = ?", key)
            _forget(ep)
        except Exception as e:
            if now < ready_at(ep) + GIVE_UP_SECONDS:
                _next_try[key] = now + RETRY_SECONDS
                log.warning("Couldn't remove sponsor reads from %s yet: %s", ep["title"], e)
                return True
            # Better an episode with its sponsor reads than one that never appears.
            log.warning("Publishing %s uncut: %s", ep["title"], e)
            with db.connect() as conn:
                conn.execute("UPDATE episodes SET sponsor_state = 'done' WHERE video_id = ? AND mode = ?", key)
            _forget(ep)
        _next_try.pop(key, None)
        return True
    return False
