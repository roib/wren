"""Chunked transcription: where the audio is split, and how chunk times are put back together."""

import types

import pytest

np = pytest.importorskip("numpy")

from wren import transcribe  # noqa: E402

RATE = transcribe.RATE


def test_split_points_fall_in_pauses():
    samples = np.ones(RATE * 100, np.float32)
    samples[RATE * 32:RATE * 33] = 0  # a pause near the 30 s mark
    samples[RATE * 58:RATE * 59] = 0  # and near 60 s
    points = transcribe.split_points(samples, chunk=RATE * 30, search=RATE * 5)
    assert points[0] == 0 and points[-1] == len(samples)
    assert RATE * 32 <= points[1] <= RATE * 33
    assert RATE * 58 <= points[2] <= RATE * 59
    assert all(b > a for a, b in zip(points, points[1:]))


def test_short_audio_is_one_chunk():
    samples = np.ones(RATE * 10, np.float32)
    assert transcribe.split_points(samples, chunk=RATE * 30, search=RATE * 5) == [0, len(samples)]


def test_chunk_times_are_offset(monkeypatch, tmp_path):
    seen = []

    class Model:
        def transcribe(self, audio, **kw):
            seen.append(len(audio))
            word = types.SimpleNamespace(start=1.0, end=1.5, word=" hi")
            return [types.SimpleNamespace(start=1.0, end=2.0, text=" hi ", words=[word])], None

    monkeypatch.setattr(transcribe, "_samples", lambda path: np.ones(RATE * 25, np.float32))
    monkeypatch.setattr(transcribe, "_whisper", lambda: Model())
    monkeypatch.setattr(transcribe, "CHUNK_SECONDS", 10)
    monkeypatch.setattr(transcribe, "split_points", lambda s: [0, RATE * 10, RATE * 20, len(s)])
    t = transcribe.transcribe(tmp_path / "x.m4a")
    assert seen == [RATE * 10, RATE * 10, RATE * 5]
    assert [s["start"] for s in t["segments"]] == [1.0, 11.0, 21.0]
    assert t["segments"][1]["words"] == [[11.0, 11.5, " hi"]]
    assert t["segments"][0]["text"] == "hi"
