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
SNAP_WINDOW = 3.0  # seconds either side of a boundary to look for a pause
MIN_PAUSE = 0.25  # seconds between words that count as a pause

RULES = """You find paid ad reads in podcast transcripts. Each line of the transcript is
[start-end seconds] text.

Find every paid sponsor read or advertisement: a stretch where a host, or an inserted ad, promotes a
third-party product or service the show is paid to promote. Typical signs: "brought to you by", "sponsored
by", "today's sponsor", promo codes, "go to example.com/showname", special offers, a switch from the
conversation into a pitch and back.

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


def transcript_text(transcript: dict) -> str:
    return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in transcript["segments"])


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


def snap(t: float, pauses: list[tuple[float, float]]) -> float:
    """The middle of the pause nearest `t`, if one is within SNAP_WINDOW; else `t`."""
    near = [(abs((a + b) / 2 - t), (a + b) / 2) for a, b in pauses if abs((a + b) / 2 - t) <= SNAP_WINDOW]
    return round(min(near)[1], 2) if near else t


def choose(ads: list[Ad], transcript: dict, duration: float | None) -> tuple[list[dict], str | None]:
    """The cuts to make from Claude's answer, and why none are made if it looks wrong."""
    pauses = _pauses(transcript)
    cuts = [{"start": snap(a.start, pauses), "end": snap(a.end, pauses), "source": "transcript",
             "advertiser": a.advertiser, "confidence": a.confidence}
            for a in ads if a.confidence == "high" and a.end > a.start]
    longest = max((c["end"] - c["start"] for c in cuts), default=0)
    if longest > MAX_AD_SECONDS:
        return [], f"not cut: one ad read was {int(longest)}s, longer than expected"
    total = sum(c["end"] - c["start"] for c in cuts)
    if duration and total > MAX_SHARE * duration:
        return [], f"not cut: {int(total)}s of ads is more than {int(MAX_SHARE * 100)}% of the episode"
    return sorted(cuts, key=lambda c: c["start"]), None
