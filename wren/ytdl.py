"""yt-dlp options shared by the downloader and the show listing."""

import shutil
import tempfile
from pathlib import Path

from .config import COOKIES_FILE

_cookies_copy: str | None = None


def _cookiefile() -> str | None:
    """A private copy of WREN_COOKIES. yt-dlp writes cookies back on exit, which
    would fail on a read-only mount (and shouldn't touch the original anyway)."""
    global _cookies_copy
    if not COOKIES_FILE or not Path(COOKIES_FILE).is_file():
        return None
    if _cookies_copy is None:
        fd, path = tempfile.mkstemp(prefix="wren-cookies-", suffix=".txt")
        with open(fd, "wb") as dst, open(COOKIES_FILE, "rb") as src:
            shutil.copyfileobj(src, dst)
        _cookies_copy = path
    return _cookies_copy


def base_opts() -> dict:
    opts = {
        # YouTube needs a JS runtime to solve its player challenges.
        "js_runtimes": {"node": {}, "deno": {}},
    }
    cookies = _cookiefile()
    if cookies:
        opts["cookiefile"] = cookies
    return opts
