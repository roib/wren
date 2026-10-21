"""An episode's description (its show notes) with the ad text taken out, and its
timestamps moved to match the cut audio.

Claude marks which lines of the description are ads (sponsor lists, promo
codes, ad-network notices) in the same request that finds the ad reads. Those
lines are dropped whole and nothing else is rewritten, so a wrong answer can
only remove a line, never change what one says.
"""

import re
from html import unescape

# A description is plain text (YouTube) or HTML (podcast feeds): split it before
# each block-level tag and after each line break, so every piece is one line as shown.
_SPLIT = re.compile(r"(?=<(?:p|li|ul|ol|div|br|h[1-6])\b)|(?=</(?:ul|ol|div)>)|(?<=\n)", re.I)
_TAG = re.compile(r"<[^>]+>")
_EMPTY_LIST = re.compile(r"<(ul|ol)>\s*</\1>", re.I)
MAX_LINES = 300  # a description longer than this is cut short in the request
MAX_CHARS = 300  # and so is each line


def _pieces(description: str) -> list[str]:
    return [p for p in _SPLIT.split(description) if p]


def _text(piece: str) -> str:
    return " ".join(unescape(_TAG.sub(" ", piece)).split())


def lines(description: str | None) -> list[str]:
    """The description's lines of text, numbered from 1 in order (tags and blank lines left out)."""
    return [t for t in (_text(p) for p in _pieces(description or "")) if t]


def numbered(description: str | None) -> str:
    """The description as the request shows it: "n: text" per line."""
    return "\n".join(f"{n}: {t[:MAX_CHARS]}" for n, t in enumerate(lines(description)[:MAX_LINES], 1))


def remove(description: str, numbers: set[int]) -> tuple[str, list[str]]:
    """The description without the given lines, and the text of the lines removed."""
    out, removed, n = [], [], 0
    for piece in _pieces(description):
        if text := _text(piece):
            n += 1
            if n in numbers:
                removed.append(text)
                continue
        out.append(piece)
    cleaned = _EMPTY_LIST.sub("", "".join(out))
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip(), removed


# A chapter line starts with its time: "12:34", "1:02:03", "00:13:45 — Title".
_STAMP = re.compile(r"^(\W*)((?:(\d{1,2}):)?(\d{1,3}):(\d{2}))\b")


def retime(description: str, segments: list[tuple[float, float]]) -> str:
    """Chapter times moved earlier by the audio cut before them, so they still
    point at the same words. A time inside a cut moves to where the cut was."""
    if not segments:
        return description

    def moved(t: float) -> float:
        return t - sum(min(end, t) - start for start, end in segments if start < t)

    def fix(m: re.Match) -> str:
        hours, minutes, seconds = m[3], m[4], m[5]
        t = round(moved(int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)))
        if hours is not None:
            h, rest = divmod(t, 3600)
            stamp = f"{h:0{len(hours)}d}:{rest // 60:02d}:{rest % 60:02d}"
        else:
            stamp = f"{t // 60:0{len(minutes)}d}:{t % 60:02d}"
        return m[1] + stamp

    out = []
    for piece in _pieces(description):
        # Only the time at the start of a line's text: a chapter list, not times mentioned in passing.
        head = re.match(r"((?:\s|<[^>]+>)*)(.*)", piece, re.S)
        out.append(head[1] + _STAMP.sub(fix, head[2], count=1))
    return "".join(out)
