"""Download a single video/episode with yt-dlp in an iOS-friendly format.

Video: H.264 + AAC in MP4 (plays natively in Safari / iOS).
Audio: AAC in .m4a, kept as-is unless AUDIO_BITRATE_KBPS is set to re-encode it.
"""

import json
import re
import subprocess
from pathlib import Path

import yt_dlp

from . import db, ytdl
from .config import AUDIO_BITRATE_KBPS, MAX_HEIGHT, MEDIA_DIR


def _video_format(max_height: int) -> str:
    # Prefer H.264 video + m4a audio; fall back to any single-file MP4, then anything.
    return (
        f"bv*[vcodec^=avc1][height<={max_height}]+ba[ext=m4a]"
        f"/b[ext=mp4][height<={max_height}]"
        f"/bv*[height<={max_height}]+ba"
        "/b"
    )


def _ydl_opts(mode: str) -> dict:
    opts = {
        **ytdl.base_opts(),
        "outtmpl": str(MEDIA_DIR / "%(id)s.%(ext)s"),
        "writethumbnail": True,
        "noplaylist": True,
        "quiet": False,
        "noprogress": True,
        # Small random pauses so one download isn't a burst of requests.
        "sleep_interval_requests": 1,
        "sleep_interval": 3,
        "max_sleep_interval": 8,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
    }
    if mode == "audio":
        opts["format"] = "ba[ext=m4a]/ba"
        opts["postprocessors"].insert(
            0, {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}
        )
    else:
        opts["format"] = _video_format(MAX_HEIGHT)
        opts["merge_output_format"] = "mp4"
        # If a fallback format landed in a non-MP4 container, convert it to MP4.
        opts["postprocessors"].insert(
            0, {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
        )
    return opts


def _media_file(info: dict) -> Path:
    downloads = info.get("requested_downloads") or []
    if downloads and downloads[0].get("filepath"):
        return Path(downloads[0]["filepath"])
    return Path(info["filepath"])


def _audio_kbps(path: Path) -> int | None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=bit_rate", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(out.stdout).get("streams") or [{}]
    rate = streams[0].get("bit_rate")
    return int(rate) // 1000 if rate else None


def _reencode_audio(path: Path, kbps: int) -> None:
    """Re-encode to AAC at `kbps`, unless it's already at or below that."""
    current = _audio_kbps(path)
    if current is not None and current <= kbps * 1.1:
        return
    tmp = path.with_suffix(".tmp.m4a")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn",
         "-c:a", "aac", "-b:a", f"{kbps}k", "-movflags", "+faststart", str(tmp)],
        check=True,
    )
    tmp.replace(path)


class Skipped(Exception):
    """The video exists but shouldn't be downloaded (live, too short, ...)."""


class Unavailable(Exception):
    """The video can't be downloaded at all: private, members-only or removed."""


_UNAVAILABLE = [
    (re.compile(r"Private video", re.I), "private video"),
    (re.compile(r"members.only|Join this channel", re.I), "members-only video"),
    (re.compile(r"Video unavailable|This video (is|has been) (unavailable|removed)|account .* terminated", re.I),
     "video unavailable"),
]


class RateLimited(Exception):
    """YouTube is throttling us (HTTP 429 or "confirm you're not a bot")."""


_RATE_LIMIT = re.compile(r"\b429\b|Too Many Requests|confirm you.re not a bot", re.I)


def is_rate_limited(err: BaseException | str) -> bool:
    return bool(_RATE_LIMIT.search(str(err)))


def _skip_reason(info: dict, min_duration: int) -> str | None:
    live = info.get("live_status")
    if live in ("is_live", "is_upcoming"):
        return "upcoming" if live == "is_upcoming" else "live"
    duration = info.get("duration")
    if min_duration and duration is not None and duration < min_duration:
        return f"too short ({int(duration)}s)"
    return None


def _thumb_file(video_id: str) -> Path | None:
    p = MEDIA_DIR / f"{video_id}.jpg"
    return p if p.exists() else None


def download(url: str, mode: str = "audio", show_id: int | None = None,
             min_duration: int = 0) -> dict:
    """Download one video and add it to the library.

    Raises Skipped (before downloading anything) if it's live, upcoming, or
    shorter than `min_duration` seconds.
    """
    if mode not in ("audio", "video"):
        raise ValueError("mode must be 'audio' or 'video'")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with yt_dlp.YoutubeDL(_ydl_opts(mode)) as ydl:
            # Look first, so skipped videos cost one metadata request and no download.
            info = ydl.extract_info(url, download=False)
            reason = _skip_reason(info, min_duration)
            if reason:
                raise Skipped(reason)
            info = ydl.process_ie_result(info, download=True)
    except yt_dlp.utils.DownloadError as e:
        if is_rate_limited(e):
            raise RateLimited(str(e)) from e
        for pattern, reason in _UNAVAILABLE:
            if pattern.search(str(e)):
                raise Unavailable(reason) from e
        raise

    media = _media_file(info)
    if mode == "audio" and AUDIO_BITRATE_KBPS > 0:
        _reencode_audio(media, AUDIO_BITRATE_KBPS)
    thumb = _thumb_file(info["id"])
    episode = {
        "video_id": info["id"],
        "mode": mode,
        "title": info.get("title") or info["id"],
        "channel": info.get("channel") or info.get("uploader"),
        "duration": int(info["duration"]) if info.get("duration") else None,
        "upload_date": info.get("upload_date"),
        "description": info.get("description"),
        "source_url": info.get("webpage_url") or url,
        "file": media.relative_to(MEDIA_DIR).as_posix(),
        "thumb": thumb.relative_to(MEDIA_DIR).as_posix() if thumb else None,
        "show_id": show_id,
    }
    db.upsert_episode(episode)
    return episode
