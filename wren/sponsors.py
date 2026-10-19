"""Cut sponsor reads out of episodes, using SponsorBlock's community marks.

Episodes of shows with skip_sponsors on are downloaded as usual but held back
from the feeds ("pending") until SPONSOR_DELAY_HOURS after YouTube published
them, by when the marks are in. Then each is processed exactly once: the marked
sponsor segments are cut out (losslessly, no re-encoding) and it joins the feed.
The file isn't changed after that, so a podcast app never holds a stale copy.

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

from . import db
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

_next_try: dict[tuple[str, str], float] = {}  # (video_id, mode) -> after a failed lookup


def ready_at(ep: dict) -> int:
    """When a held-back episode is due: the delay after YouTube published it
    (or after it was downloaded, if the publish time isn't known)."""
    return int((ep.get("published_at") or ep["added_at"]) + SPONSOR_DELAY_HOURS * 3600)


def lookup(video_id: str) -> list[tuple[float, float]]:
    """The video's sponsor segments as (start, end) seconds. [] if none are
    marked; raises if SponsorBlock can't be reached."""
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    url = API + prefix + "?" + urllib.parse.urlencode({"categories": json.dumps(CATEGORIES)})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            videos = json.load(res)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # nothing marked for any video with this prefix
            return []
        raise
    video = next((v for v in videos if v.get("videoID") == video_id), None)
    return [tuple(s["segment"]) for s in (video or {}).get("segments", [])
            if s.get("actionType", "skip") == "skip"]


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


def process(ep: dict) -> None:
    """Look up and cut one held-back episode, then let it into the feeds."""
    segments = merge(lookup(ep["video_id"]), ep.get("duration"))
    if segments:
        cut(MEDIA_DIR / ep["file"], segments)
    removed = round(sum(end - start for start, end in segments))
    with db.connect() as conn:
        conn.execute(
            """UPDATE episodes SET sponsor_state = 'done', sponsor_removed = ?, sponsor_segments = ?,
                                   duration = CASE WHEN duration IS NULL THEN NULL ELSE MAX(0, duration - ?) END
               WHERE video_id = ? AND mode = ?""",
            (removed, json.dumps([[round(a, 1), round(b, 1)] for a, b in segments]), removed,
             ep["video_id"], ep["mode"]))
    if segments:
        log.info("Removed %d sponsor read(s), %ds, from %s (%s)", len(segments), removed, ep["title"], ep["mode"])
    else:
        log.info("No sponsor reads marked in %s (%s)", ep["title"], ep["mode"])


def process_next(now: float | None = None) -> bool:
    """Process the oldest held-back episode that's due. Returns False when none is."""
    now = now or time.time()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM episodes WHERE sponsor_state = 'pending' ORDER BY added_at")]
    for ep in rows:
        key = (ep["video_id"], ep["mode"])
        if ready_at(ep) > now or _next_try.get(key, 0) > now:
            continue
        try:
            process(ep)
        except Exception as e:
            if now < ready_at(ep) + GIVE_UP_SECONDS:
                _next_try[key] = now + RETRY_SECONDS
                log.warning("Couldn't remove sponsor reads from %s yet: %s", ep["title"], e)
                return True
            # Better an episode with its sponsor reads than one that never appears.
            log.warning("Publishing %s uncut: %s", ep["title"], e)
            with db.connect() as conn:
                conn.execute("UPDATE episodes SET sponsor_state = 'done' WHERE video_id = ? AND mode = ?", key)
        _next_try.pop(key, None)
        return True
    return False
