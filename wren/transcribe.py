"""Local speech-to-text with Whisper (faster-whisper), for finding ad reads.

Optional: installed in the container image, not needed elsewhere. Audio is
decoded with ffmpeg rather than faster-whisper's own decoder (PyAV), whose
version pairing broke in testing; ffmpeg is already here for downloads.
"""

import json
import subprocess
import threading
import time
from pathlib import Path

from .config import TRANSCRIPT_DAYS, TRANSCRIPT_DIR, WHISPER_MODEL, WHISPER_THREADS

_model = None
_lock = threading.Lock()


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _whisper():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=WHISPER_THREADS)
    return _model


def _samples(path: Path):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-vn", "-f", "f32le", "-ac", "1",
                          "-ar", "16000", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


RATE = 16000
CHUNK_SECONDS = 600  # Whisper's working memory grows with the audio it's given: about 3.8 GB for an hour
QUIET_SEARCH = 15  # seconds either side of each chunk boundary to look for a pause


def split_points(samples, chunk: int = CHUNK_SECONDS * RATE, search: int = QUIET_SEARCH * RATE) -> list[int]:
    """Where to cut the audio into chunks: near every `chunk` samples, at the
    quietest half second within `search`, so a cut falls in a pause, not a word."""
    import numpy as np
    window = RATE // 2
    points, pos = [0], chunk
    while pos < len(samples) - window:
        lo, hi = max(points[-1] + window, pos - search), min(len(samples) - window, pos + search)
        frames = (hi - lo) // window
        if frames < 1:
            break
        energy = np.square(samples[lo:lo + frames * window]).reshape(frames, window).mean(axis=1)
        points.append(lo + int(energy.argmin()) * window + window // 2)
        pos = points[-1] + chunk
    return points + [len(samples)]


def transcribe(path: Path) -> dict:
    """{"segments": [{"start", "end", "text", "words": [[start, end, word], ...]}], "seconds": time taken}.
    Done in chunks of about CHUNK_SECONDS, so memory stays flat for long episodes."""
    t0 = time.time()
    samples = _samples(path)
    model = _whisper()
    segs = []
    points = split_points(samples)
    for start, end in zip(points, points[1:]):
        offset = start / RATE
        segments, _ = model.transcribe(samples[start:end], language="en", word_timestamps=True, vad_filter=True,
                                       beam_size=1, condition_on_previous_text=False)
        # (float(): Whisper's times are NumPy numbers.)
        segs += [{"start": round(float(s.start) + offset, 2), "end": round(float(s.end) + offset, 2),
                  "text": s.text.strip(),
                  "words": [[round(float(w.start) + offset, 2), round(float(w.end) + offset, 2), w.word]
                            for w in (s.words or [])]}
                 for s in segments]
    return {"segments": segs, "seconds": round(time.time() - t0, 1)}


def path_for(video_id: str) -> Path:
    return TRANSCRIPT_DIR / f"{video_id}.json"


def save(video_id: str, transcript: dict) -> None:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path_for(video_id).write_text(json.dumps(transcript))


def load(video_id: str) -> dict | None:
    try:
        return json.loads(path_for(video_id).read_text())
    except (OSError, ValueError):
        return None


def clean_up() -> int:
    """Delete transcripts older than TRANSCRIPT_DAYS. Returns how many."""
    cutoff = time.time() - TRANSCRIPT_DAYS * 86400
    old = [p for p in TRANSCRIPT_DIR.glob("*.json") if p.stat().st_mtime < cutoff] if TRANSCRIPT_DIR.exists() else []
    for p in old:
        p.unlink(missing_ok=True)
    return len(old)
