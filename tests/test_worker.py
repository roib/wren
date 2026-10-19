import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from wren import db, downloader, push, worker

TZ = ZoneInfo("UTC")


def at(hour, minute=0):
    return dt.datetime(2026, 3, 10, hour, minute, tzinfo=TZ).timestamp()


@pytest.fixture
def hours(monkeypatch):
    monkeypatch.setattr(worker, "TIMEZONE", TZ)
    return lambda start, end: monkeypatch.setattr(worker, "ACTIVE_HOURS", (start, end))


def test_active_hours(hours):
    hours(7, 23)
    assert worker.is_active(at(7))
    assert worker.is_active(at(22, 59))
    assert not worker.is_active(at(23))
    assert not worker.is_active(at(3))


def test_active_hours_past_midnight(hours):
    hours(22, 6)
    assert worker.is_active(at(23))
    assert worker.is_active(at(5))
    assert not worker.is_active(at(12))


def test_always_active(hours):
    hours(0, 24)
    assert worker.is_active(at(3))


def test_next_active(hours):
    hours(7, 23)
    assert worker.next_active(at(12)) == at(12)
    assert worker.next_active(at(3)) == at(7)
    assert worker.next_active(at(23, 30)) == at(7) + 86400


def test_manual_job_success(fake_downloader):
    job = worker.add_job("https://www.youtube.com/watch?v=abc", "audio")
    assert worker._run_next_job()
    done = worker.list_jobs()[0]
    assert (done["id"], done["status"], done["title"]) == (job, "done", "Episode abc")
    assert not worker._run_next_job()


def test_manual_job_failure_alerts(fake_downloader, sent):
    worker.add_job("https://www.youtube.com/watch?v=bad", "video")
    fake_downloader.fail["bad"] = RuntimeError("ERROR: [youtube] bad: Video unavailable")
    worker._run_next_job()
    assert worker.list_jobs()[0]["status"] == "error"
    assert sent[0]["title"] == "Couldn't add an episode"
    assert "Video unavailable" in sent[0]["body"]


def test_manual_job_rate_limited_stays_queued(fake_downloader):
    worker.add_job("https://www.youtube.com/watch?v=x", "audio")
    fake_downloader.fail["x"] = downloader.RateLimited("429")
    with pytest.raises(downloader.RateLimited):
        worker._run_next_job()
    assert worker.list_jobs()[0]["status"] == "queued"


def test_recover_requeues_interrupted_work():
    job = worker.add_job("https://www.youtube.com/watch?v=x", "audio")
    worker._update_job(job, status="running")
    old = worker.add_job("https://www.youtube.com/watch?v=y", "audio")
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'done', updated_at = 0 WHERE id = ?", (old,))
    worker._recover()
    assert [(j["id"], j["status"]) for j in worker.list_jobs()] == [(job, "queued")]


def test_status_and_download_now():
    w = worker.Worker()
    w.download_now(7)
    status = w.status()
    assert status["activity"] == "idle"
    assert status["override"] and status["override_shows"] == [7]
    assert status["paused_until"] is None
    assert push.active_count() == 0


def test_worker_cuts_sponsor_reads_before_anything_else(monkeypatch):
    from wren import sponsors
    calls = []
    monkeypatch.setattr(sponsors, "process_next", lambda now: calls.append(now) or True)
    w = worker.Worker()
    w.next_poll = float("inf")
    w.paused_until = float("inf")  # even while YouTube downloads are paused
    assert w._step()
    assert len(calls) == 1
