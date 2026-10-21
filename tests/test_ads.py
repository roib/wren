"""Finding ad reads in transcripts (transcript mode), with Whisper and Claude faked."""

import json
import types

import pytest
from conftest import item

from wren import ads, chapters, db, sponsors, subscriptions, transcribe
from wren.config import MEDIA_DIR

SHOW = "https://www.youtube.com/playlist?list=UUtest"


def words(*spans):
    """A transcript of one word per (start, end) span."""
    return {"segments": [{"start": a, "end": b, "text": f"w{i}", "words": [[a, b, f" w{i}"]]}
                         for i, (a, b) in enumerate(spans)], "seconds": 1.0}


# Speech with pauses at 99-101, 159.5-160.5 and 300-301.
TRANSCRIPT = words((0, 99), (101, 159.5), (160.5, 300), (301, 600))


@pytest.fixture
def transcript_mode(monkeypatch):
    """Transcript mode on: SponsorBlock, Whisper and Claude are fakes the test controls."""
    calls = {"lookup": [], "transcribe": [], "claude": [], "cut": [], "chapters": []}
    fake = types.SimpleNamespace(marks={}, answer=[], description_lines=[], transcript=TRANSCRIPT, calls=calls,
                                 chapters=chapters.Chapters(chapters=[]))
    monkeypatch.setattr(ads, "available", lambda: True)
    monkeypatch.setattr(transcribe, "available", lambda: True)
    monkeypatch.setattr(sponsors, "lookup", lambda vid: calls["lookup"].append(vid) or fake.marks.get(vid, []))
    monkeypatch.setattr(transcribe, "transcribe", lambda path: calls["transcribe"].append(path.name) or fake.transcript)
    monkeypatch.setattr(ads, "ask_claude", lambda t, title, vid, description=None: calls["claude"].append(title)
                        or ads.Ads(ads=fake.answer, description_lines=fake.description_lines))
    monkeypatch.setattr(sponsors, "cut", lambda path, segs: calls["cut"].append((path.name, segs)))
    monkeypatch.setattr(chapters, "ask", lambda t, title, vid: fake.chapters() if callable(fake.chapters)
                        else fake.chapters)
    monkeypatch.setattr(chapters, "write", lambda path, chs, duration: calls["chapters"].append((path.name, chs)))
    sponsors._next_try.clear()
    return fake


@pytest.fixture
def held(fake_feeds, fake_downloader):
    """A show removing sponsor reads, with one downloaded episode waiting."""
    fake_feeds.set(SHOW, "Test Show", [item("e1")])
    s = subscriptions.subscribe(SHOW, backfill=1)
    subscriptions.update_show(s["id"], skip_sponsors=True)
    fake_downloader.published["e1"] = 1_000_000
    subscriptions.download_next()
    return s


def episode(mode="audio"):
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM episodes WHERE video_id = 'e1' AND mode = ?", (mode,)).fetchone())


def ad(start, end, confidence="high", advertiser="Acme"):
    return ads.Ad(start=start, end=end, advertiser=advertiser, confidence=confidence)


def test_snap_moves_boundaries_outwards_into_pauses():
    pauses = ads._pauses(TRANSCRIPT)
    assert pauses == [(99, 101), (159.5, 160.5), (300, 301)]
    assert ads.snap_start(101, pauses) == 100      # the pause before the ad's first word
    assert ads.snap_start(99.4, pauses) == 100     # already in it
    assert ads.snap_start(162, pauses) == 160      # back to the pause just before
    assert ads.snap_start(98.5, pauses) == 98.5    # never forward into the ad, to a pause after its start
    assert ads.snap_end(159.5, pauses) == 160      # the pause after its last word
    assert ads.snap_end(158, pauses) == 160        # forward to the pause just after
    assert ads.snap_end(102, pauses) == 102        # never back into the ad, to a pause before its end
    assert ads.snap_end(200, pauses) == 200        # no pause close by: left alone


def test_snap_stays_near_the_speech_in_a_long_pause():
    pauses = [(10.0, 20.0)]
    assert ads.snap_start(20.0, pauses) == 18.5
    assert ads.snap_end(10.0, pauses) == 11.5


def test_choose_marks_promos():
    promo = ads.Ad(start=99.4, end=159.6, advertiser="The Daily Thing", kind="promo", confidence="high")
    cuts, _ = ads.choose([promo, ad(300, 310)], TRANSCRIPT, 600)
    assert [c.get("kind") for c in cuts] == ["promo", None]  # no kind: a sponsor read
    assert cuts[0]["advertiser"] == "The Daily Thing"
    assert "kind" in ads.SCHEMA["properties"]["ads"]["items"]["required"]


def test_cut_starts_at_the_ads_first_words_not_the_line():
    # One transcript line ran the guest's answer into the ad: "...like yours? Absolutely. You're watching..."
    said = "families like yours absolutely you're watching an interview subscribe now back to the interview so"
    spoken = {"segments": [{"start": 0, "end": 20, "text": said, "words": [
        [i * 1.0 + (1.5 if i >= 4 else 0), i * 1.0 + 0.8 + (1.5 if i >= 4 else 0), f" {w}"]
        for i, w in enumerate(said.split())]}], "seconds": 1.0}
    found = ads.Ad(start=0, end=16.3, advertiser="The Show", kind="promo", confidence="high",
                   first_words="You're watching an interview", last_words="back to the interview")
    cuts, _ = ads.choose([found], spoken, 600)
    # The cut starts in the pause before "you're" (5.5s), not at the line's start, and ends after "interview".
    assert 3.8 < cuts[0]["start"] < 5.5 and 15.3 <= cuts[0]["end"] < 16.5
    # Words that can't be found leave the times as given.
    lost = found.model_copy(update={"first_words": "nothing like this", "last_words": ""})
    assert ads.refine(lost, ads._spoken(spoken)) == (0, 16.3)


def test_choose_keeps_only_confident_and_plausible_cuts():
    cuts, note = ads.choose([ad(99.4, 159.6, advertiser="Acme"), ad(400, 410, "low", "Joke")], TRANSCRIPT, 600)
    assert note is None
    assert cuts == [{"start": 100, "end": 160, "source": "transcript", "advertiser": "Acme", "confidence": "high"}]
    assert ads.choose([ad(0, 300)], TRANSCRIPT, 3600)[1].startswith("not cut: one ad read was 300s")
    assert ads.choose([ad(0, 200), ad(300, 500)], TRANSCRIPT, 2000)[1].endswith("more than 15% of the episode")


def test_cuts_right_after_downloading(held, transcript_mode):
    transcript_mode.answer = [ad(99.4, 159.6), ad(400, 410, "low")]
    # No eight-hour wait: due as soon as it's downloaded.
    assert sponsors.process_next(now=episode()["added_at"])
    calls = transcript_mode.calls
    assert calls["lookup"] == ["e1"] and calls["transcribe"] == ["e1.m4a"] and calls["claude"] == ["Episode e1"]
    assert calls["cut"] == [("e1.m4a", [(100, 160)])]
    ep = episode()
    assert (ep["sponsor_state"], ep["sponsor_removed"]) == ("done", 60)
    assert json.loads(ep["sponsor_cuts"]) == {"note": None, "cuts": [
        {"start": 100, "end": 160, "source": "transcript", "advertiser": "Acme", "confidence": "high"}]}
    assert transcribe.load("e1") == TRANSCRIPT  # kept, to check the cut afterwards


def test_sponsorblock_marks_and_transcript_finds_are_both_cut(held, transcript_mode):
    # Marked so far: one ad. The transcript finds another nobody has marked yet.
    transcript_mode.marks["e1"] = [(30, 90)]
    transcript_mode.answer = [ad(99.4, 159.6, advertiser="Acme")]
    sponsors.process_next(now=episode()["added_at"])
    assert transcript_mode.calls["transcribe"] == ["e1.m4a"]
    assert json.loads(episode()["sponsor_cuts"])["cuts"] == [
        {"start": 30, "end": 90, "source": "sponsorblock"},
        {"start": 100, "end": 160, "source": "transcript", "advertiser": "Acme", "confidence": "high"}]
    assert transcript_mode.calls["cut"] == [("e1.m4a", [(30, 90), (100, 160)])]


def test_overlapping_finds_keep_sponsorblocks_boundaries():
    marks = [{"start": 95, "end": 170, "source": "sponsorblock"}]
    found = [{"start": 100, "end": 130, "source": "transcript", "advertiser": "Hims", "confidence": "high"},
             {"start": 130, "end": 168, "source": "transcript", "advertiser": "Upwork", "confidence": "high"}]
    assert sponsors.combine(marks, found) == [
        {"start": 95, "end": 170, "source": "sponsorblock", "advertiser": "Hims, Upwork"}]
    assert sponsors.combine(marks, []) == marks


def test_set_aside_transcript_still_cuts_the_marks(held, transcript_mode):
    transcript_mode.marks["e1"] = [(30, 90)]
    transcript_mode.answer = [ad(0, 300)]  # implausible: set aside
    sponsors.process_next(now=episode()["added_at"])
    decision = json.loads(episode()["sponsor_cuts"])
    assert decision["cuts"] == [{"start": 30, "end": 90, "source": "sponsorblock"}]
    assert decision["note"].startswith("not cut")


def test_sponsorblock_down_falls_back_to_the_transcript(held, transcript_mode, monkeypatch):
    def down(vid):
        raise OSError("unreachable")
    monkeypatch.setattr(sponsors, "lookup", down)
    transcript_mode.answer = [ad(99.4, 159.6)]
    sponsors.process_next(now=episode()["added_at"])
    assert episode()["sponsor_removed"] == 60


def test_video_copy_reuses_the_audio_copys_cuts(held, transcript_mode, fake_downloader):
    transcript_mode.answer = [ad(99.4, 159.6)]
    sponsors.process_next(now=episode()["added_at"])
    subscriptions.update_show(held["id"], video_copy=True)
    subscriptions.download_next_copy()
    for calls in transcript_mode.calls.values():
        calls.clear()
    sponsors.process_next(now=episode("video")["added_at"])
    calls = transcript_mode.calls
    assert calls["lookup"] == [] and calls["transcribe"] == [] and calls["claude"] == []
    assert calls["cut"] == [("e1.mp4", [(100, 160)])]


def test_implausible_answer_publishes_uncut_with_a_note(held, transcript_mode):
    transcript_mode.answer = [ad(0, 300)]
    sponsors.process_next(now=episode()["added_at"])
    ep = episode()
    assert (ep["sponsor_state"], ep["sponsor_removed"]) == ("done", 0)
    assert json.loads(ep["sponsor_cuts"])["note"].startswith("not cut")
    assert transcript_mode.calls["cut"] == []


def test_ad_lines_leave_the_description(held, transcript_mode):
    with db.connect() as conn:
        conn.execute("UPDATE episodes SET description = ? WHERE video_id = 'e1'",
                     ("About bridges.\nSPONSORS:\n- Acme: code SHOW\n\n01:00 Intro\n03:00 Bridges",))
    transcript_mode.answer = [ad(99.4, 159.6)]
    transcript_mode.description_lines = [2, 3]
    sponsors.process_next(now=episode()["added_at"])
    ep = episode()
    # The sponsor lines are gone, and chapter times after the 60s cut moved back.
    assert ep["description"] == "About bridges.\n\n01:00 Intro\n02:00 Bridges"
    assert json.loads(ep["sponsor_cuts"])["description_removed"] == ["SPONSORS:", "- Acme: code SHOW"]


def test_chapters_are_made_with_the_cuts(held, transcript_mode):
    from fastapi.testclient import TestClient

    from wren import server
    client = TestClient(server.app)
    transcript_mode.answer = [ad(99.4, 159.6)]  # cut 100-160
    transcript_mode.chapters = chapters.Chapters(chapters=[
        chapters.Chapter(start=0, first_words="w0", title="Opening"),
        chapters.Chapter(start=300, first_words="w3", title="Later on")])  # its first word is at 301
    sponsors.process_next(now=episode()["added_at"])
    # Written into the file on the cut audio's timeline: 300.5 less the 60s cut.
    assert transcript_mode.calls["chapters"] == [("e1.m4a", [{"start": 0.0, "title": "Opening"},
                                                             {"start": 240.5, "title": "Later on"}])]
    decision = json.loads(episode()["sponsor_cuts"])
    assert decision["chapters_source"] == "transcript" and decision["chapters"][1]["start"] == 300.5
    # Linked from the feed, for apps that read chapters there.
    feed = client.get("/feeds/test-show.xml").text
    assert 'podcast:chapters url="http://testserver/chapters/audio/e1.json"' in feed
    assert client.get("/chapters/audio/e1.json").json()["chapters"][1] == {"startTime": 240.5, "title": "Later on"}
    assert client.get("/chapters/video/e1.json").status_code == 404


def test_episode_waits_for_both_answers(held, transcript_mode):
    def pending():
        raise ads.Pending("e1-chapters")
    transcript_mode.chapters = pending
    added = episode()["added_at"]
    assert sponsors.process_next(now=added)
    assert episode()["sponsor_state"] == "pending"  # the ad reads are in, the chapters aren't
    transcript_mode.chapters = chapters.Chapters(chapters=[chapters.Chapter(start=0, title="All of it")])
    sponsors.process_next(now=added + sponsors.ANSWER_POLL_SECONDS)
    assert episode()["sponsor_state"] == "done"


def test_chapters_failing_doesnt_hold_the_episode(held, transcript_mode):
    def fail():
        raise ads.DetectError("no answer")
    transcript_mode.chapters = fail
    sponsors.process_next(now=episode()["added_at"])
    assert episode()["sponsor_state"] == "done" and "chapters" not in json.loads(episode()["sponsor_cuts"])


def test_claude_unreachable_retries_later(held, transcript_mode, monkeypatch):
    def fail(t, title, vid):
        raise ads.DetectError("no answer")
    monkeypatch.setattr(ads, "ask_claude", fail)
    added = episode()["added_at"]
    assert sponsors.process_next(now=added)
    assert episode()["sponsor_state"] == "pending"
    # The transcript was kept, so the retry doesn't transcribe again.
    monkeypatch.setattr(ads, "ask_claude", lambda t, title, vid, description=None: ads.Ads(ads=[]))
    sponsors.process_next(now=added + sponsors.RETRY_SECONDS)
    assert episode()["sponsor_state"] == "done"
    assert transcript_mode.calls["transcribe"] == ["e1.m4a"]


def test_transcript_is_deleted_with_the_episode(held, transcript_mode):
    sponsors.process_next(now=episode()["added_at"])
    assert transcribe.path_for("e1").exists()
    subscriptions.delete_episode("e1")
    assert not transcribe.path_for("e1").exists()


def test_without_a_key_it_waits_for_sponsorblock(held):
    assert sponsors.mode() == "sponsorblock"
    assert sponsors.ready_at(episode()) == 1_000_000 + sponsors.SPONSOR_DELAY_HOURS * 3600


def test_ask_claude_request(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    sent = {}

    class Messages:
        def parse(self, **kwargs):
            sent.update(kwargs)
            return types.SimpleNamespace(stop_reason="end_turn", parsed_output=ads.Ads(ads=[ad(1, 2)]), _request_id="r")
    monkeypatch.setattr(anthropic, "Anthropic", lambda: types.SimpleNamespace(messages=Messages()))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert ads.ask_claude(words((0, 5)), "Show #1", "x").ads == [ad(1, 2)]
    assert sent["model"] == "claude-haiku-5-5" and sent["output_format"] is ads.Ads and sent["system"] == ads.RULES
    assert "[0.0-5.0] w0" in sent["messages"][0]["content"]


def test_old_transcripts_are_cleaned_up(tmp_path):
    transcribe.save("old", {"segments": []})
    transcribe.save("new", {"segments": []})
    import os
    os.utime(transcribe.path_for("old"), (0, 0))
    assert transcribe.clean_up() == 1
    assert not transcribe.path_for("old").exists() and transcribe.path_for("new").exists()
    assert MEDIA_DIR.exists()


def test_transcript_lines_follow_sentences():
    t = {"segments": [{"start": 0, "end": 9, "text": "x", "words": [
        [0.0, 0.5, " That's"], [0.5, 1.0, " 15%"], [1.0, 1.5, " off."], [1.6, 2.0, " Did"], [2.0, 2.4, " you"],
        [2.4, 3.0, " see?"], [3.0, 3.5, " So"], [5.0, 5.5, " anyway"]]},
        {"start": 9, "end": 10, "text": "No word times here", "words": []}]}
    assert ads.transcript_text(t).splitlines() == [
        "[0.0-1.5] That's 15% off.",   # the sentence ends the line...
        "[1.6-3.0] Did you see?",
        "[3.0-3.5] So",                # ...and so does a pause of a second or more
        "[5.0-5.5] anyway",
        "[9.0-10.0] No word times here",
    ]


def test_prompt_counts_other_podcasts_promos():
    # Including the publisher's own: its other podcasts and its subscriptions.
    assert "Promotions for any other podcast" in ads.RULES and "network's or platform's own" in ads.RULES
    assert "own paid products" in ads.RULES
