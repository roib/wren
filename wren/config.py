import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WREN_DATA", ROOT / "data"))
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "wren.db"
WEB_DIR = Path(os.environ.get("WREN_WEB", ROOT / "web"))

MAX_HEIGHT = int(os.environ.get("WREN_MAX_HEIGHT", "720"))
# 0 keeps YouTube's original audio stream; set e.g. 64 to re-encode smaller files.
AUDIO_BITRATE_KBPS = int(os.environ.get("WREN_AUDIO_KBPS", "0"))

# Subscriptions
DOWNLOAD_PAUSE_SECONDS = int(os.environ.get("WREN_DOWNLOAD_PAUSE", "5"))
MAX_ATTEMPTS = 5  # failed downloads are retried on later syncs up to this many times
POLL_MINUTES = int(os.environ.get("WREN_POLL_MINUTES", "60"))
# Set WREN_WORKER=0 to run the server without background downloads/polling.
WORKER_ENABLED = os.environ.get("WREN_WORKER", "1") != "0"
# Optional Netscape-format cookies file for yt-dlp, if YouTube starts asking
# to "confirm you're not a bot".
COOKIES_FILE = os.environ.get("WREN_COOKIES") or None
