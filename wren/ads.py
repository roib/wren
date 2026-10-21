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
import re
import time
from typing import Literal

from pydantic import BaseModel

from . import notes
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

These count as ads too, with "high" confidence when they're clearly a promo:
- Promotions for any other podcast or show, including the same publisher's, network's or platform's own
  ("from iHeartRadio, listen to...", a news outlet's trailer for its other podcasts).
- Pitches for the publisher's, network's or platform's own paid products: a newspaper or magazine
  subscription, an ad-free or premium podcast tier, an app, events.

Do NOT count: the hosts briefly mentioning their own merchandise, tours, live shows, Patreon or
membership; other episodes of this same show; social media, "like and subscribe"; or products discussed
naturally as part of the conversation without being a pitch.

For each ad give "start" = the start time of its first line and "end" = the end time of its last line,
in seconds, so that cutting [start, end] removes the whole ad and nothing else. Include the lead-in
("let's take a quick break, this episode is brought to you by...") and the hand-back if they're part of
the ad. A line can hold the end of the conversation and the start of an ad (or the other way round), so
also give "first_words": the ad's first five words exactly as in the transcript, and "last_words": its
last five words. Give the advertiser; the kind: "sponsor" for a paid read for a third party, "promo" for a
promotion of another show or of the publisher's, network's or platform's own products; and your
confidence that it is an ad: "high", "medium" or "low".
If there are no ads, return an empty list.

After the transcript comes the episode's description, one numbered line per line. In "description_lines"
give the numbers of its lines that are ads by the same rules: sponsor lists and their headings
("SPONSORS:", "Thanks to our sponsors"), offers and promo codes, promotions of other shows or of the
publisher's own products, ad-network notices ("Learn more about your ad choices..."), and chapter lines
that only mark an ad break. Keep every other line, including the hosts' own links, merchandise, Patreon
and social media. With no description, or no ads in it, give an empty list."""


class Ad(BaseModel):
    start: float
    end: float
    advertiser: str
    kind: Literal["sponsor", "promo"] = "sponsor"  # answers from before promos were told apart have none
    confidence: Literal["high", "medium", "low"]
    first_words: str = ""
    last_words: str = ""


class Ads(BaseModel):
    ads: list[Ad]
    description_lines: list[int] = []  # numbered as in notes.lines()


class DetectError(Exception):
    """Claude couldn't be asked, or declined to answer."""


class Pending(Exception):
    """Asked the host helper; its answer isn't there yet."""


# The answer's shape, for `claude -p --json-schema` (kept flat: no $refs).
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["ads", "description_lines"],
    "properties": {
        "ads": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["start", "end", "advertiser", "kind", "confidence", "first_words", "last_words"],
            "properties": {"start": {"type": "number"}, "end": {"type": "number"}, "advertiser": {"type": "string"},
                           "kind": {"type": "string", "enum": ["sponsor", "promo"]},
                           "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                           "first_words": {"type": "string"}, "last_words": {"type": "string"}}}},
        "description_lines": {"type": "array", "items": {"type": "integer"}}},
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


def _prompt(transcript: dict, title: str, description: str | None) -> str:
    return (f"Episode: {title}\n\nTRANSCRIPT\n{transcript_text(transcript)}\n\n"
            f"DESCRIPTION\n{notes.numbered(description) or '(none)'}")


def ask_claude(transcript: dict, title: str, video_id: str, description: str | None = None) -> Ads:
    """Claude's answer: the ad reads, and the description's ad lines. Raises
    Pending while the host helper is on it."""
    return ask(video_id, RULES, SCHEMA, Ads, _prompt(transcript, title, description))


def ask(request_id: str, system: str, schema: dict, output: type[BaseModel], prompt: str):
    """One question to Claude, answered as `output`. Through the host helper the
    answer is kept until forget(request_id), so an episode waiting on a second
    question doesn't ask the first one again."""
    if backend() == "claude-cli":
        return _ask_helper(request_id, system, schema, output, prompt)
    return _ask_api(system, output, prompt)


def forget(request_id: str) -> None:
    """Done with a question: its request and answer files go."""
    (ADS_DIR / "answers" / f"{request_id}.json").unlink(missing_ok=True)
    (ADS_DIR / "requests" / f"{request_id}.json").unlink(missing_ok=True)


def _write(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)  # never a half-written file for the other side


def _ask_helper(request_id: str, system: str, schema: dict, output: type[BaseModel], prompt: str):
    answer_path = ADS_DIR / "answers" / f"{request_id}.json"
    request_path = ADS_DIR / "requests" / f"{request_id}.json"
    if answer_path.exists():
        answer = json.loads(answer_path.read_text())
        if not answer.get("ok"):
            forget(request_id)  # asked again on the retry
            raise DetectError(f"helper: {answer.get('error') or 'failed'}")
        return output.model_validate(answer["result"])
    if not request_path.exists():
        _write(request_path, {"id": request_id, "model": ADS_MODEL, "system": system, "schema": schema,
                              "prompt": prompt, "created_at": int(time.time())})
    raise Pending(request_id)


def _ask_api(system: str, output: type[BaseModel], prompt: str):
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=ADS_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_format=output,
    )
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise DetectError(f"no answer ({response.stop_reason}, request {response._request_id})")
    return response.parsed_output


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


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("’", "").replace("'", ""))


def _spoken(transcript: dict) -> list[tuple[float, float, str]]:
    """Every word with its times, as lowercase tokens ("You're" is "youre")."""
    return [(w[0], w[1], t) for s in transcript["segments"] for w in s.get("words") or [] for t in _tokens(w[2])]


def _find(spoken: list, phrase: str, lo: float, hi: float) -> list[tuple[float, float]]:
    """Where the phrase is said starting between lo and hi: (its first word's start, its last word's end)."""
    want = _tokens(phrase)
    n = len(want)
    if not n:
        return []
    return [(spoken[i][0], spoken[i + n - 1][1]) for i in range(len(spoken) - n + 1)
            if lo <= spoken[i][0] <= hi and [w[2] for w in spoken[i:i + n]] == want]


def refine(ad: Ad, spoken: list) -> tuple[float, float]:
    """The ad's start and end at its first and last words, where they're found near
    the times given: a transcript line can run the conversation into the ad, and
    a line's start time would cut the conversation too."""
    start, end = ad.start, ad.end
    if hits := _find(spoken, ad.first_words, ad.start - SNAP_WINDOW, ad.end):
        start = hits[0][0]
    if hits := _find(spoken, ad.last_words, start, ad.end + SNAP_WINDOW):
        end = hits[-1][1]
    return start, end


def choose(ads: list[Ad], transcript: dict, duration: float | None) -> tuple[list[dict], str | None]:
    """The cuts to make from Claude's answer, and why none are made if it looks wrong."""
    pauses, spoken = _pauses(transcript), _spoken(transcript)
    ads = [a.model_copy(update=dict(zip(("start", "end"), refine(a, spoken)))) for a in ads]
    # A promo is marked as one; a cut without a kind is a sponsor read (as all were before).
    cuts = [{"start": snap_start(a.start, pauses), "end": snap_end(a.end, pauses), "source": "transcript",
             "advertiser": a.advertiser, **({"kind": "promo"} if a.kind == "promo" else {}),
             "confidence": a.confidence}
            for a in ads if a.confidence == "high" and a.end > a.start]
    longest = max((c["end"] - c["start"] for c in cuts), default=0)
    if longest > MAX_AD_SECONDS:
        return [], f"not cut: one ad read was {int(longest)}s, longer than expected"
    total = sum(c["end"] - c["start"] for c in cuts)
    if duration and total > MAX_SHARE * duration:
        return [], f"not cut: {int(total)}s of ads is more than {int(MAX_SHARE * 100)}% of the episode"
    return sorted(cuts, key=lambda c: c["start"]), None
