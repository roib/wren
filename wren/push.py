"""Web Push alerts to the home-screen app: Wren tells you when something needs
attention (YouTube blocking downloads, a download failing for good, a show
that can't be checked...). Each problem is announced once (see alert())."""

import json
import logging
import re
import threading
import time

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush

from . import db
from .config import DATA_DIR

log = logging.getLogger("wren")

KEY_FILE = DATA_DIR / "vapid_private.pem"  # Wren's push signing key, made once
_vapid: Vapid02 | None = None
_lock = threading.Lock()


def _key() -> Vapid02:
    global _vapid
    with _lock:
        if _vapid is None:
            if KEY_FILE.exists():
                _vapid = Vapid02.from_file(str(KEY_FILE))
            else:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                _vapid = Vapid02()
                _vapid.generate_keys()
                _vapid.save_key(str(KEY_FILE))
                log.info("Made a Web Push key")
    return _vapid


def public_key() -> str:
    """The key the app subscribes with (applicationServerKey)."""
    raw = _key().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64urlencode(raw)


def subscribe(sub: dict, origin: str) -> None:
    keys = sub.get("keys") or {}
    if not sub.get("endpoint") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("incomplete push subscription")
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO push_subscriptions (endpoint, p256dh, auth, origin, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth,
                                                   origin = excluded.origin""",
            (sub["endpoint"], keys["p256dh"], keys["auth"], origin, int(time.time())))


def unsubscribe(endpoint: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


def _send_all(payload: dict) -> None:
    with db.connect() as conn:
        subs = [dict(r) for r in conn.execute("SELECT * FROM push_subscriptions")]
    for s in subs:
        try:
            webpush(
                {"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                data=json.dumps(payload),
                vapid_private_key=_key(),
                # Apple wants a contact; the app's own address will do.
                vapid_claims={"sub": s["origin"] or "mailto:wren@localhost"},
                ttl=24 * 3600,
                timeout=15,
            )
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):  # unsubscribed or expired: forget it (403 is a key problem, keep it)
                unsubscribe(s["endpoint"])
                log.info("Dropped a push subscription (HTTP %s)", status)
            else:
                log.warning("Push failed: %s", e)
        except Exception as e:
            log.warning("Push failed: %s", e)


def send(title: str, body: str, url: str = "/#/status", tag: str | None = None) -> None:
    """Notify every subscribed device, in the background."""
    payload = {"title": title, "body": body, "url": url, "tag": tag}
    threading.Thread(target=_send_all, args=(payload,), daemon=True).start()


def alert(key: str, title: str, body: str, url: str = "/#/status") -> bool:
    """Send an alert unless one with this key was already sent. Keys name a
    problem ("showerror:7"); clear() it when the problem goes away so it can be
    announced again if it comes back. Returns True if sent. With no devices
    signed up nothing is recorded, so turning alerts on later still reports
    problems that are still going on."""
    if not subscriber_count():
        return False
    with db.connect() as conn:
        new = conn.execute("INSERT OR IGNORE INTO alerts (key, sent_at) VALUES (?, ?)",
                           (key, int(time.time()))).rowcount
    if new:
        log.info("Alert: %s: %s", title, body)
        send(title, body, url, tag=key)
    return bool(new)


def clear(key: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM alerts WHERE key = ?", (key,))


def short_error(err) -> str:
    """yt-dlp's messages without the "ERROR: [youtube] abc123:" prefix."""
    text = str(err).splitlines()[0] if str(err) else "unknown error"
    return re.sub(r"^(feed: )?ERROR:\s*(\[[\w:]+\]\s*[\w-]+:\s*)?", "", text)[:160]


def subscriber_count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]
