import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ODDO_DATA", ROOT / "data"))
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "oddo.db"
WEB_DIR = ROOT / "web"

MAX_HEIGHT = int(os.environ.get("ODDO_MAX_HEIGHT", "720"))
AUDIO_BITRATE_KBPS = int(os.environ.get("ODDO_AUDIO_KBPS", "64"))
