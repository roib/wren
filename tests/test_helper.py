"""The host helper path: Wren's request/answer files, and the helper script
itself run against a fake `claude`."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from wren import ads, push, worker
from wren.config import ADS_DIR

SCRIPT = Path(__file__).resolve().parent.parent / "host" / "wren-claude-helper"
TRANSCRIPT = {"segments": [{"start": 0.0, "end": 5.0, "text": "brought to you by Acme", "words": []}]}


def heartbeat(ok=True, age=0, error=None):
    ADS_DIR.mkdir(parents=True, exist_ok=True)
    (ADS_DIR / "helper.json").write_text(json.dumps({"at": time.time() - age, "ok": ok, "error": error}))


def test_backend_choice(monkeypatch):
    assert ads.backend() is None  # no helper, no key
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(ads, "_api_available", lambda: True)
    assert ads.backend() == "api"
    heartbeat()
    assert ads.backend() == "claude-cli"  # the helper wins while it's running
    heartbeat(age=ads.HELPER_STALE + 1)
    assert ads.backend() == "api"
    heartbeat(ok=False, error="not logged in")
    assert ads.backend() == "api"
    monkeypatch.setattr(ads, "ADS_BACKEND", "claude-cli")
    assert ads.backend() is None  # told to use only the helper


def test_ask_helper_writes_a_request_then_reads_the_answer():
    heartbeat()
    with pytest.raises(ads.Pending):
        ads.ask_claude(TRANSCRIPT, "Show #1", "abc_123-XY")
    request = json.loads((ADS_DIR / "requests" / "abc_123-XY.json").read_text())
    assert request["schema"] == ads.SCHEMA and request["system"] == ads.RULES
    assert "[0.0-5.0] brought to you by Acme" in request["prompt"]
    with pytest.raises(ads.Pending):  # asked once, not again
        ads.ask_claude(TRANSCRIPT, "Show #1", "abc_123-XY")

    (ADS_DIR / "answers").mkdir(parents=True, exist_ok=True)
    (ADS_DIR / "answers" / "abc_123-XY.json").write_text(json.dumps(
        {"ok": True, "result": {"ads": [{"start": 0, "end": 5, "advertiser": "Acme", "confidence": "high"}]}}))
    assert ads.ask_claude(TRANSCRIPT, "Show #1", "abc_123-XY") == [ads.Ad(start=0, end=5, advertiser="Acme",
                                                                       confidence="high")]
    assert not any((ADS_DIR / "requests").iterdir()) and not any((ADS_DIR / "answers").iterdir())


def test_a_failed_answer_is_an_error():
    heartbeat()
    (ADS_DIR / "answers").mkdir(parents=True)
    (ADS_DIR / "answers" / "v1.json").write_text(json.dumps({"ok": False, "error": "overloaded"}))
    with pytest.raises(ads.DetectError, match="overloaded"):
        ads.ask_claude(TRANSCRIPT, "t", "v1")


def test_alert_when_the_helper_stops(sent):
    worker.check_helper()
    assert sent == []  # never set up: nothing to say
    heartbeat(ok=False, error="Claude Code on the server isn't logged in")
    worker.check_helper()
    assert sent[0]["title"] == "Claude on the server isn't answering"
    assert sent[0]["body"].startswith("Claude Code on the server isn't logged in.")
    heartbeat()
    worker.check_helper()
    assert push.active_count() == 0


# ---- the script itself ----------------------------------------------------------

FAKE_CLAUDE = """#!/bin/sh
# Stands in for Claude Code: logged in (or not), and answers from a canned reply.
case "$1" in
  --version) echo "9.9.9 (Claude Code)"; exit 0 ;;
  auth) [ -e "$FAKE_DIR/logged-out" ] && echo '{"loggedIn": false}' || echo '{"loggedIn": true}'; exit 0 ;;
esac
printf '%s\\n' "$@" > "$FAKE_DIR/args"
cat > "$FAKE_DIR/stdin"
ls -A > "$FAKE_DIR/cwd-contents"
cat "$FAKE_DIR/reply"
"""


@pytest.fixture
def helper(tmp_path):
    if not all(shutil.which(t) for t in ("jq", "flock", "timeout")):
        pytest.skip("needs jq, flock and timeout")
    fake = tmp_path / "fake"
    fake.mkdir()
    claude = fake / "claude"
    claude.write_text(FAKE_CLAUDE)
    claude.chmod(0o755)
    data = tmp_path / "data"

    def run(reply=None):
        (fake / "reply").write_text(json.dumps(reply or {}))
        env = {**os.environ, "CLAUDE": str(claude), "FAKE_DIR": str(fake)}
        subprocess.run([str(SCRIPT), str(data)], env=env, check=True, timeout=60)
        return json.loads((data / "ads" / "helper.json").read_text())
    return types_namespace(run=run, data=data, fake=fake)


def types_namespace(**kw):
    import types
    return types.SimpleNamespace(**kw)


def request(data, video_id, **fields):
    path = data / "ads" / "requests" / f"{video_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": video_id, "model": "claude-haiku-5-5", "system": "RULES",
                                "schema": ads.SCHEMA, "prompt": "[0.0-5.0] hello", **fields}))


def test_script_answers_a_request(helper):
    request(helper.data, "abc123")
    status = helper.run({"type": "result", "is_error": False, "total_cost_usd": 0.01,
                         "structured_output": {"ads": [{"start": 1, "end": 2, "advertiser": "A", "confidence": "high"}]}})
    assert status["ok"] is True and status["claude"] == "9.9.9 (Claude Code)"
    answer = json.loads((helper.data / "ads" / "answers" / "abc123.json").read_text())
    assert answer["ok"] is True and answer["result"]["ads"][0]["advertiser"] == "A"
    args = (helper.fake / "args").read_text().splitlines()
    assert args[:5] == ["-p", "--model", "claude-haiku-5-5", "--tools", ""]  # every tool off
    assert json.loads(args[args.index("--json-schema") + 1]) == ads.SCHEMA
    assert args[args.index("--system-prompt") + 1] == "RULES"
    assert (helper.fake / "stdin").read_text().strip() == "[0.0-5.0] hello"
    assert (helper.fake / "cwd-contents").read_text() == ""  # ran in an empty directory


def test_script_reports_a_failed_request(helper):
    request(helper.data, "abc123")
    status = helper.run({"type": "result", "is_error": True, "result": "API Error: overloaded"})
    assert status["ok"] is True  # the helper is fine; this request failed
    answer = json.loads((helper.data / "ads" / "answers" / "abc123.json").read_text())
    assert answer == {"ok": False, "error": "API Error: overloaded"}


def test_script_reports_being_logged_out(helper):
    (helper.fake / "logged-out").touch()
    request(helper.data, "abc123")
    status = helper.run()
    assert status["ok"] is False and "isn't logged in" in status["error"]
    assert not (helper.data / "ads" / "answers" / "abc123.json").exists()


def test_script_ignores_files_that_arent_requests(helper):
    request(helper.data, "../../etc")  # lands as requests/../../etc.json: outside, untouched
    bad = helper.data / "ads" / "requests" / "a b.json"
    bad.write_text("{}")
    helper.run({"is_error": False, "structured_output": {"ads": []}})
    assert not bad.exists()
    assert list((helper.data / "ads" / "answers").iterdir()) == []
