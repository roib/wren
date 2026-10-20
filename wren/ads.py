"""Find ad reads in a transcript with Claude, for episodes nobody has marked.

Tested against SponsorBlock's marks on six episodes before building this: it
found 97% of the marked sponsor time, boundaries within a few seconds, and its
one wrong pick (a joke about a sponsor) came back "low" confidence, so only
"high" ranges are cut. Each boundary is moved to the nearest pause between
words, and a result that looks implausible isn't cut at all: cutting
conversation is worse than leaving an ad in.
"""

import json
import os
import time
from typing import Literal

from pydantic import BaseModel

from .config import ADS_BACKEND, ADS_DIR, ADS_MODEL

MAX_AD_SECONDS = 4 * 60  # longer than any single read seen; likely a mistake
MAX_SHARE = 0.15  # of the episode
SNAP_WINDOW = 3.0  # how far outside the ad to look for the pause around it
SNAP_TOLERANCE = 0.3  # a boundary this close to a pause counts as at it
SNAP_MARGIN = 1.5  # in a long pause, cut at most this far from the speech
MIN_PAUSE = 0.25  # seconds between words that count as a pause

RULES = """You find paid ad reads in podcast transcripts. Each line of the transcript is
[start-end seconds] text.

Find every paid sponsor read or advertisement: a stretch where a host, or an inserted ad, promotes a
third-party product or service the show is paid to promote. Typical signs: "brought to you by", "sponsored
by", "today's sponsor", promo codes, "go to example.com/showname", special offers, a switch from the
conversation into a pitch and back.

Promotions for other podcasts or shows, by other people or networks ("from iHeartRadio, listen to..."),
count as ads too.

Do NOT count: the show's own merchandise, tours, live shows, Patreon or membership, other episodes or
podcasts by the same people, social media, "like and subscribe"; or products discussed naturally as part
of the conversation without being a paid pitch.

For each ad give "start" = the start time of its first line and "end" = the end time of its last line,
in seconds, so that cutting [start, end] removes the whole ad and nothing else. Include the lead-in
("let's take a quick break, this episode is brought to you by...") and the hand-back if they're part of
the ad. Give the advertiser, and your confidence that it is a paid ad: "high", "medium" or "low".
If there are no ads, return an empty list."""


class Ad(BaseModel):
    start: float
    end: float
    advertiser: str
    confidence: Literal["high", "medium", "low"]


class Ads(BaseModel):
    ads: list[Ad]


class DetectError(Exception):
    """Claude couldn't be asked, or declined to answer."""


class Pending(Exception):
    """Asked the host helper; its answer isn't there yet."""


# The answer's shape, for `claude -p --json-schema` (kept flat: no $refs).
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["ads"],
    "properties": {"ads": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["start", "end", "advertiser", "confidence"],
        "properties": {"start": {"type": "number"}, "end": {"type": "number"}, "advertiser": {"type": "string"},
                       "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}}},
}

HELPER_STALE = 30 * 60  # the helper refreshes helper.json every 10 minutes


def _api_available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def helper_status() -> dict | None:
    """The host helper's last report ({"at", "ok", "error"}), or None if it was never set up."""
    try:
        return json.loads((ADS_DIR / "helper.json").read_text())
    except (OSError, ValueError):
        return None


def helper_ok() -> bool:
    status = helper_status()
    return bool(status and status.get("ok") and time.time() - status.get("at", 0) < HELPER_STALE)


def backend() -> str | None:
    """How Claude will be asked right now: "claude-cli", "api", or None."""
    if ADS_BACKEND in ("auto", "claude-cli") and helper_ok():
        return "claude-cli"
    if ADS_BACKEND in ("auto", "api") and _api_available():
        return "api"
    return None


def available() -> bool:
    return backend() is not None


LINE_PAUSE = 1.0  # seconds of silence that end a line
LINE_WORDS = 45  # and the longest a line gets


def transcript_text(transcript: dict) -> str:
    """The transcript as "[start-end] text" lines, one per sentence where the
    word times allow. Whisper's own segments can run an ad's last words and the
    conversation after it together ("...for 15% off. Did you see..."), and the
    answer's boundaries can only be as fine as these lines."""
    lines, line = [], []

    def flush():
        if line:
            lines.append(f"[{line[0][0]:.1f}-{line[-1][1]:.1f}] " + " ".join(w[2].strip() for w in line))
            line.clear()

    for seg in transcript["segments"]:
        if not seg.get("words"):  # no word times: the segment as it is
            flush()
            lines.append(f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}")
            continue
        for w in seg["words"]:
            if line and (w[0] - line[-1][1] >= LINE_PAUSE or len(line) >= LINE_WORDS):
                flush()
            line.append(w)
            if w[2].strip().rstrip("\"')”’").endswith((".", "?", "!")):
                flush()
    flush()
    return "\n".join(lines)


def ask_claude(transcript: dict, title: str, video_id: str) -> list[Ad]:
    """Claude's list of ad reads. Raises Pending while the host helper is on it."""
    if backend() == "claude-cli":
        return _ask_helper(transcript, title, video_id)
    return _ask_api(transcript, title)


def _write(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)  # never a half-written file for the other side


def _ask_helper(transcript: dict, title: str, video_id: str) -> list[Ad]:
    answer_path = ADS_DIR / "answers" / f"{video_id}.json"
    request_path = ADS_DIR / "requests" / f"{video_id}.json"
    if answer_path.exists():
        answer = json.loads(answer_path.read_text())
        answer_path.unlink(missing_ok=True)
        request_path.unlink(missing_ok=True)
        if not answer.get("ok"):
            raise DetectError(f"helper: {answer.get('error') or 'failed'}")
        return Ads.model_validate(answer["result"]).ads
    if not request_path.exists():
        _write(request_path, {"id": video_id, "model": ADS_MODEL, "system": RULES, "schema": SCHEMA,
                              "prompt": f"Episode: {title}\n\nTRANSCRIPT\n{transcript_text(transcript)}",
                              "created_at": int(time.time())})
    raise Pending(video_id)


def _ask_api(transcript: dict, title: str) -> list[Ad]:
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=ADS_MODEL,
        max_tokens=16000,
        system=RULES,
        messages=[{"role": "user", "content": f"Episode: {title}\n\nTRANSCRIPT\n{transcript_text(transcript)}"}],
        output_format=Ads,
    )
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise DetectError(f"no answer ({response.stop_reason}, request {response._request_id})")
    return response.parsed_output.ads


def _pauses(transcript: dict) -> list[tuple[float, float]]:
    words = [w for s in transcript["segments"] for w in s.get("words", [])]
    return [(a[1], b[0]) for a, b in zip(words, words[1:]) if b[0] - a[1] >= MIN_PAUSE]


def snap_start(t: float, pauses: list[tuple[float, float]]) -> float:
    """Move an ad's start back into the pause before its first word (never
    forward into the ad: its own sentences have pauses too). Unchanged if
    there's no pause there."""
    before = [(a, b) for a, b in pauses if a <= t + SNAP_TOLERANCE and b >= t - SNAP_WINDOW]
    if not before:
        return t
    a, b = max(before)
    return round(max((a + b) / 2, b - SNAP_MARGIN), 2)


def snap_end(t: float, pauses: list[tuple[float, float]]) -> float:
    """Move an ad's end forward into the pause after its last word."""
    after = [(a, b) for a, b in pauses if b >= t - SNAP_TOLERANCE and a <= t + SNAP_WINDOW]
    if not after:
        return t
    a, b = min(after)
    return round(min((a + b) / 2, a + SNAP_MARGIN), 2)


def choose(ads: list[Ad], transcript: dict, duration: float | None) -> tuple[list[dict], str | None]:
    """The cuts to make from Claude's answer, and why none are made if it looks wrong."""
    pauses = _pauses(transcript)
    cuts = [{"start": snap_start(a.start, pauses), "end": snap_end(a.end, pauses), "source": "transcript",
             "advertiser": a.advertiser, "confidence": a.confidence}
            for a in ads if a.confidence == "high" and a.end > a.start]
    longest = max((c["end"] - c["start"] for c in cuts), default=0)
    if longest > MAX_AD_SECONDS:
        return [], f"not cut: one ad read was {int(longest)}s, longer than expected"
    total = sum(c["end"] - c["start"] for c in cuts)
    if duration and total > MAX_SHARE * duration:
        return [], f"not cut: {int(total)}s of ads is more than {int(MAX_SHARE * 100)}% of the episode"
    return sorted(cuts, key=lambda c: c["start"]), None
