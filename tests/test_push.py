from wren import push


def test_alert_dedupes_while_open(sent):
    assert push.alert("showerror:1", "Show: can't check", "boom")
    assert not push.alert("showerror:1", "Show: can't check", "boom again")
    assert push.active_count() == 1
    assert len(sent) == 1
    push.resolve("showerror:1")
    assert push.active_count() == 0
    assert push.alert("showerror:1", "Show: can't check", "back")  # raised again when it comes back
    assert len(sent) == 2


def test_resolve_matching_escapes_wildcards():
    push.alert("failed:1:a", "t", "b")
    push.alert("failed:1:b", "t", "b")
    push.alert("failed:10:a", "t", "b")
    push.alert("failedX1:a", "t", "b")
    push.alert("fail_d:1:a", "t", "b")
    push.resolve_matching("fail_d:")  # "_" is literal, not "any character"
    assert {a["key"] for a in push.listing()["active"]} == {"failed:1:a", "failed:1:b", "failed:10:a", "failedX1:a"}
    push.resolve_matching("failed:1:", keep={"failed:1:b"})
    assert {a["key"] for a in push.listing()["active"]} == {"failed:1:b", "failed:10:a", "failedX1:a"}


def test_dismiss_and_listing():
    push.alert("a", "A", "body")
    push.alert("b", "B", "body")
    first = next(a for a in push.listing()["active"] if a["key"] == "a")
    push.dismiss(first["id"])
    listing = push.listing()
    assert [a["key"] for a in listing["active"]] == ["b"]
    assert [a["key"] for a in listing["recent"]] == ["a"]
    push.dismiss()
    assert push.active_count() == 0


def test_short_error():
    assert push.short_error("ERROR: [youtube] abc-123: Video unavailable\nmore") == "Video unavailable"
    assert push.short_error("feed: ERROR: [youtube:tab] UUx: Gone") == "Gone"
    assert push.short_error("") == "unknown error"
    assert len(push.short_error("x" * 500)) == 160


def test_subscriptions():
    sub = {"endpoint": "https://push.example/1", "keys": {"p256dh": "p", "auth": "a"}}
    push.subscribe(sub, "https://wren.example")
    push.subscribe({**sub, "keys": {"p256dh": "p2", "auth": "a2"}}, "https://wren.example")
    assert push.subscriber_count() == 1
    push.unsubscribe(sub["endpoint"])
    assert push.subscriber_count() == 0


def test_public_key_is_made_once():
    key = push.public_key()
    assert push.KEY_FILE.exists()
    push._vapid = None
    assert push.public_key() == key
