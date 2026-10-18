import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WREN_DATA", ROOT / "data"))
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "wren.db"
WEB_DIR = Path(os.environ.get("WREN_WEB", ROOT / "web"))

MAX_HEIGHT = int(os.environ.get("WREN_MAX_HEIGHT", "720"))
# 0 keeps YouTube's original audio stream; set e.g. 64 to re-encode smaller files.
AUDIO_BITRATE_KBPS = int(os.environ.get("WREN_AUDIO_KBPS", "0"))

# Subscriptions
MAX_ATTEMPTS = 5  # failed downloads are retried on later syncs up to this many times


def _range(value: str) -> tuple[int, int]:
    """Parse "60-120" (or "90") into (low, high)."""
    lo, _, hi = value.partition("-")
    return int(lo), int(hi or lo)


# Show checks: every POLL_MINUTES, shifted randomly by up to POLL_JITTER_MINUTES.
POLL_MINUTES = int(os.environ.get("WREN_POLL_MINUTES", "120"))
POLL_JITTER_MINUTES = 15

# Automatic checks and subscription downloads only happen during these hours
# (start inclusive, end exclusive; "22-6" wraps past midnight; "0-24" is always).
ACTIVE_HOURS = _range(os.environ.get("WREN_ACTIVE_HOURS", "7-23"))
try:
    TIMEZONE = ZoneInfo(os.environ.get("WREN_TZ") or "America/Los_Angeles")
except ZoneInfoNotFoundError:
    TIMEZONE = ZoneInfo("UTC")

# Go easy on YouTube: a random pause between subscription downloads, a cap per
# hour, and a pause on everything when YouTube starts rate-limiting.
DOWNLOAD_PAUSE = _range(os.environ.get("WREN_DOWNLOAD_PAUSE", "60-120"))
MAX_DOWNLOADS_PER_HOUR = int(os.environ.get("WREN_MAX_DOWNLOADS_PER_HOUR", "6"))
BLOCK_PAUSE_MINUTES = int(os.environ.get("WREN_BLOCK_PAUSE_MINUTES", "120"))

# Set WREN_WORKER=0 to run the server without background downloads/polling.
WORKER_ENABLED = os.environ.get("WREN_WORKER", "1") != "0"
# Optional Netscape-format cookies file for yt-dlp, if YouTube starts asking
# to "confirm you're not a bot".
COOKIES_FILE = os.environ.get("WREN_COOKIES") or None


# Public base URL for links in podcast feeds (e.g. https://wren.example.com).
# Unset, it's worked out from each request (Host + X-Forwarded-Proto).
BASE_URL = os.environ.get("WREN_BASE_URL", "").rstrip("/")
