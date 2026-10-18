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
                KEY_FILE.chmod(0o600)
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
    payload = {"title": title, "body": body, "url": url, "tag": tag, "badge": active_count()}
    threading.Thread(target=_send_all, args=(payload,), daemon=True).start()


# ---- the alert log --------------------------------------------------------------------

def alert(key: str, title: str, body: str, url: str = "/#/status") -> bool:
    """Record a problem and notify devices, unless the same problem (same key)
    is already open. resolve() it when it goes away, so it's raised again if it
    comes back. Returns True if it was new."""
    now = int(time.time())
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM alert_log WHERE key = ? AND resolved_at IS NULL", (key,)).fetchone():
            return False
        conn.execute("INSERT INTO alert_log (key, title, body, url, created_at) VALUES (?, ?, ?, ?, ?)",
                     (key, title, body, url, now))
    log.info("Alert: %s: %s", title, body)
    send(title, body, url, tag=key)
    return True


def resolve(key: str) -> None:
    """The problem went away."""
    resolve_matching(key)


def resolve_matching(prefix: str, keep: set[str] = frozenset()) -> None:
    """Resolve open alerts whose key starts with `prefix`, except those in `keep`."""
    with db.connect() as conn:
        rows = conn.execute("SELECT id, key FROM alert_log WHERE resolved_at IS NULL AND key LIKE ? ESCAPE '\\'",
                            (prefix.replace("%", "\\%").replace("_", "\\_") + "%",)).fetchall()
        ids = [r["id"] for r in rows if r["key"] not in keep]
        if ids:
            conn.execute(f"UPDATE alert_log SET resolved_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                         (int(time.time()), *ids))


def dismiss(alert_id: int | None = None) -> None:
    """Mark one open alert (or all of them) as dealt with."""
    with db.connect() as conn:
        if alert_id is None:
            conn.execute("UPDATE alert_log SET resolved_at = ? WHERE resolved_at IS NULL", (int(time.time()),))
        else:
            conn.execute("UPDATE alert_log SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL",
                         (int(time.time()), alert_id))


def active_count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM alert_log WHERE resolved_at IS NULL").fetchone()[0]


HISTORY_DAYS = 14


def listing() -> dict:
    """Open alerts, newest first, and those resolved in the last two weeks."""
    with db.connect() as conn:
        active = [dict(r) for r in conn.execute(
            "SELECT * FROM alert_log WHERE resolved_at IS NULL ORDER BY created_at DESC")]
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM alert_log WHERE resolved_at >= ? ORDER BY resolved_at DESC LIMIT 50",
            (int(time.time()) - HISTORY_DAYS * 86400,))]
        conn.execute("DELETE FROM alert_log WHERE resolved_at < ?", (int(time.time()) - 90 * 86400,))
    return {"active": active, "recent": recent}


def short_error(err) -> str:
    """yt-dlp's messages without the "ERROR: [youtube] abc123:" prefix."""
    text = str(err).splitlines()[0] if str(err) else "unknown error"
    return re.sub(r"^(feed: )?ERROR:\s*(\[[\w:]+\]\s*[\w-]+:\s*)?", "", text)[:160]


def subscriber_count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]
