<p align="center">
  <img src="web/favicon.svg" alt="Wren" width="112">
</p>

<h1 align="center">Wren</h1>

A self-hosted downloader that turns YouTube channels and playlists into private
podcast feeds. Subscribe to a show, and Wren downloads new episodes as audio
(or video) on a polite schedule and publishes an RSS feed you can follow in
Apple Podcasts or any other podcast app.

Wren comes with a small web app for managing it, built to feel at home as an
iPhone home-screen app.

## Why

Mostly: **no injected ads.** The audio a podcast's own feed serves is often
stitched together at download time by an ad network, with ads chosen for you
and dropped into the episode, and frequently the same ones over and over.
Many shows also put full episodes on YouTube, where YouTube shows its ads
around the video rather than inside the file. Wren downloads that file, so an
episode comes without the inserted breaks. Ad reads the hosts record as part
of the show are still there.

It also helps with:

- **Shows that are only on YouTube** can be listened to like any other
  podcast.
- **Background listening without YouTube Premium:** episodes play in Apple
  Podcasts with the screen locked, offline, at any speed, with the queue
  and resume you're used to.
- **Video podcasts** can be watched in a podcast app, from a separate video
  feed.

## What it does

- **Follows shows.** Subscribe to a YouTube channel or playlist, then choose
  audio or video and how many recent episodes to fetch. New episodes are
  picked up automatically.
- **Downloads in podcast-friendly formats.** Audio is AAC (`.m4a`), kept as
  YouTube serves it. Video is H.264/AAC MP4, up to 720p by default.
- **Publishes podcast feeds.** Each show has a feed at `/feeds/<show>.xml`.
  Shows that also download video get a second one at `/feeds/<show>-video.xml`.
  Single episodes you add by hand go in `/feeds/singles.xml`. Each show also
  gets square artwork made from its channel picture, with a small Wren badge
  so you can tell it apart from the official feed.
- **Goes easy on YouTube.**
  - Checks shows every couple of hours, during set hours only.
  - Spaces downloads apart and caps them per hour.
  - Backs off for a while when YouTube starts rate-limiting.
- **Filters out noise.**
  - Skips Shorts and clips with a minimum length.
  - Can download only titles matching a filter.
  - Skips private and members-only videos without retrying them.
- **Tidies up.** Keeps the latest N episodes of each show (or everything).
- **Raises alerts.** Shows problems, and sends Web Push notifications, when
  downloads are paused, an episode fails for good, a show can't be checked
  or goes quiet, disk space runs low, or yt-dlp couldn't update.

## The app

| Screen | What's there |
|---|---|
| **Shows** | Downloader status, then every show with its artwork, an Audio/Video badge, its size and what it's doing |
| **Show** | Open in Podcasts, Copy feed, Check now / Download now, episodes in progress, downloaded, failed and skipped, a test player, and the show's settings |
| **Add** | Paste any YouTube link. A channel or playlist shows its latest episodes; a single video offers that episode, its playlist or its whole channel |
| **Alerts** | Problems that need attention now, recently resolved ones, and the switch for notifications |
| **Status** | The downloader's schedule and queue, recent single episodes, storage and the yt-dlp version |

### On an iPhone

- **Install it.** Open Wren in Safari and choose Share → Add to Home Screen.
- **Listen in a podcast app.** On a show's page, tap *Open in Podcasts*, or
  use *Copy feed* and then Library → ⋯ → *Follow a Show by URL* in Apple
  Podcasts. Apple Podcasts fetches the feed from the phone itself, so a
  server that's only reachable on your home network works.
- **Share to Wren.** A Shortcut can put Wren in the share sheet of Safari and
  the YouTube app; the Add page has step-by-step instructions. It opens
  `/#/add?url=<link>` with the link already looked up.
- **Get notifications.** Turn them on from the Alerts page. Push only works
  in the home-screen app, not in a Safari tab.

## Running it

Wren is one container. It needs a volume for its data and nothing else:

```sh
podman build -t wren .
podman run -d --name wren -p 8000:8000 -v wren-data:/data wren
```

Then open `http://<host>:8000`. Put it behind HTTPS (for example nginx with a
certificate) for push notifications, the clipboard buttons and the iPhone
app to work.

On every start, the container installs the newest yt-dlp release, because
YouTube changes often. If PyPI can't be reached, it uses the copy built into
the image. Restarting it now and then (say, weekly) keeps yt-dlp current.
With Podman, `podman generate systemd --new --name wren` makes a systemd
unit that recreates the container on each start.

### Configuration

Wren is configured with environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `WREN_DATA` | `./data` (`/data` in the container) | Database, media, artwork and the push key |
| `WREN_TZ` | `America/Los_Angeles` | Time zone for active hours |
| `WREN_ACTIVE_HOURS` | `7-23` | Hours when shows are checked and downloaded (`22-6` wraps past midnight, `0-24` is always) |
| `WREN_POLL_MINUTES` | `120` | How often shows are checked (±15 min) |
| `WREN_DOWNLOAD_PAUSE` | `60-120` | Random pause between downloads, in seconds |
| `WREN_MAX_DOWNLOADS_PER_HOUR` | `6` | Cap on subscription downloads per hour |
| `WREN_BLOCK_PAUSE_MINUTES` | `120` | How long to pause when YouTube rate-limits |
| `WREN_MAX_HEIGHT` | `720` | Maximum video height |
| `WREN_AUDIO_KBPS` | `0` | Re-encode audio to this bitrate (`0` keeps the original) |
| `WREN_COOKIES` | unset | Netscape cookies file for yt-dlp, if YouTube asks to confirm you're not a bot |
| `WREN_BASE_URL` | from each request | Public address used in feed links |
| `WREN_WORKER` | `1` | `0` runs the server without background checks and downloads |
| `WREN_UPDATE_YTDLP` | `1` | `0` skips updating yt-dlp when the container starts |

The **Download now** buttons in the app ignore active hours, but the pause
between downloads, the hourly cap and rate-limit backoff still apply.

## Command line

```sh
wren serve [--host 0.0.0.0] [--port 8000]   # the server, app and feeds
wren subscribe <url> [--mode audio|video] [--keep 10] [--backfill 3]
                     [--min-duration 300] [--title-filter REGEX]
wren shows                                   # list subscriptions
wren sync [show_id]                          # check and download now
wren unsubscribe <show_id>                   # episodes stay, as single episodes
wren add <url> [--mode audio|video]          # download one episode
wren list                                    # list downloaded episodes
```

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e .
WREN_WORKER=0 .venv/bin/wren serve --host 127.0.0.1 --port 8000
```

You also need `ffmpeg` and a JavaScript runtime (Deno or Node) for yt-dlp's
YouTube support.

How it's put together:

- **Server:** FastAPI (`wren/server.py`). The app is plain HTML, CSS and
  JavaScript in `web/`, with no build step.
- **Storage:** SQLite in `WREN_DATA/wren.db`, with media files alongside it in
  `WREN_DATA/media`.
- **Background work:** one worker thread (`wren/worker.py`) checks shows and
  downloads, one episode at a time.
- **Show listings:** `wren/feeds.py` uses yt-dlp's flat playlist listing. A
  channel is followed through its uploads playlist; other playlists are
  sorted newest first.
- **Feeds:** `wren/podcast.py` builds the RSS. An empty feed gets a short
  placeholder episode, because Apple Podcasts won't follow a feed with no
  episodes.
- **Alerts:** `wren/push.py` keeps the alert log and sends Web Push
  (VAPID via `pywebpush`).
