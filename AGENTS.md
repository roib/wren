# AGENTS.md

Notes for coding agents working on Wren. Read this before changing anything;
README.md covers what Wren is and how to use it.

## What it is

A self-hosted YouTube-to-podcast downloader: subscribe to channels and
playlists, Wren downloads episodes (audio, optionally video) on a polite
schedule and publishes private RSS feeds that the owner follows in **Apple
Podcasts**. The web app is a **manager**, not a player: shows, episodes, state,
settings, alerts. It runs as an iPhone home-screen app and, at ≥1024 px, as a
three-pane desktop layout.

## Layout

| Path | What |
|---|---|
| `wren/server.py` | FastAPI app: JSON API (`/api/...`), feeds (`/feeds/...`), media (`/media/...`), static app (`web/`) |
| `wren/worker.py` | The single background thread: feed checks, downloads, pacing, rate-limit pauses, daily health checks |
| `wren/subscriptions.py` | Shows and feed items: subscribe, check a feed, download queue, video copies, retention, delete |
| `wren/feeds.py` | Show listings via yt-dlp flat playlist extraction (channels → their `UU…` uploads playlist) |
| `wren/downloader.py` | One download with yt-dlp; `Skipped`, `Unavailable`, `RateLimited` exceptions |
| `wren/podcast.py` | RSS rendering, feed slugs, the empty-feed placeholder |
| `wren/artwork.py` | Square show artwork from the channel picture, with the Wren badge (`wren-badge.png`) |
| `wren/admin.py` | Read models for the app (`/api/overview`, show detail, singles) and link resolving (`/api/resolve`) |
| `wren/push.py` | Alert log (`alert_log` table) and Web Push (VAPID key in `$WREN_DATA/vapid_private.pem`) |
| `wren/db.py` | SQLite schema + `_migrate()` (add-column migrations); WAL mode |
| `wren/config.py` | All `WREN_*` settings (documented in README) |
| `wren/cli.py` | `wren serve / subscribe / shows / sync / add / list / unsubscribe` |
| `web/` | The app: `index.html`, `app.js`, `style.css`, `sw.js` (push only), icons. No build step, no framework |
| `start.sh` | Container entrypoint: installs the newest yt-dlp into `/tmp/ytdlp`, then `wren serve` |
| `tests/` | pytest suite; `conftest.py` fakes YouTube listings, downloads and push, so it runs offline |
| `docs/screenshots/` | README screenshots (made-up sample data only) |

## Conventions

- **Match the surrounding code.** Comments explain *why*, in plain sentences;
  keep their density. Small functions, no new dependencies without a reason.
- **Front end:** vanilla JS with the `h(tag, props, ...children)` helper; pages
  re-render from fetched JSON (`load()` → `render()`), polling faster while busy.
  Icons are inline SVG strings in `ICONS`. Hash routes (`#/show/7`, `#/add`,
  `#/alerts`, `#/status`, `#/singles`); in-app links replace history entries.
- **Keep the iPhone feel:** the custom fast-tap handler (activate on
  `touchend`, ignore taps that only stop a scroll), push/pop view transitions
  (fade on desktop), scroll memory, the sticky nav title, no zoom/rubber-band on
  the page. Buttons that need a real user gesture (notification permission)
  carry `data-native-tap`.
- **Responsive, one codebase:** phone layout by default; `@media (min-width:
  1024px)` adds the sidebar and show list (`#sidebar`, `#listpane`, built in
  `renderPanes()`); `isDesktop()` mirrors the breakpoint in JS.
- **Styling:** monochrome tokens on `:root` with a dark-mode override; Audio
  badges outlined, Video filled. Keep light and dark both working.
- **Copy:** plain language, no jargon in the UI ("Downloads paused", not "429").
- **Database changes:** add columns in `SCHEMA` *and* in `_migrate()`; never
  rewrite or drop user data in a migration.

## Things learned the hard way

- **Playback stays in podcast apps.** iOS home-screen web apps can't resume
  audio from the lock screen after a pause (WebKit bug 243258); silent-audio and
  Web Audio keepalives both failed on device. Don't rebuild the in-app player.
- **YouTube rate-limits.** Heavy test downloads get an IP blocked (429 / "not a
  bot"). Keep the pacing (`DOWNLOAD_PAUSE`, hourly cap, `BLOCK_PAUSE_MINUTES`).
  Test downloads with short videos (e.g. `jNQXAC9IVRw`) and sparingly.
- **yt-dlp flat listings** have no dates (use `approximate_date`, already set)
  and playlists keep private/deleted videos as untitled placeholders (filtered
  out). Non-uploads playlists are sorted newest first against the channel's
  uploads. Subtitles are deliberately not downloaded (they were rate-limited
  separately and failed whole downloads).
- **Apple Podcasts** won't follow an empty feed (hence the placeholder item),
  caches artwork (artwork URLs carry `?v=<mtime>`), and opens `podcast://` links
  with "Follow a Show by URL" filled in.
- **Safari caching:** the server sends `Cache-Control: no-cache` for everything
  except `/media/`. `sw.js` must stay push-only; it also deletes the caches an
  old player version left on phones.
- **Web Push:** only forget a subscription on HTTP 404/410 (Apple returns 403
  for key problems). Alerts dedupe by key while unresolved; resolve them when
  the problem goes away. Linux Chromium builds can't subscribe at all.
- **iOS icons** are fixed when added to the home screen; icon URLs carry
  `?v=bird` to defeat Safari's cache.

## Running and testing locally

```sh
python -m venv .venv && .venv/bin/pip install -e '.[e2e]'   # or '.[test]' without the browser tests
.venv/bin/playwright install webkit chromium
.venv/bin/pytest                # everything; -m "not e2e" for just the back end
WREN_DATA=/tmp/wren-test WREN_WORKER=0 .venv/bin/wren serve --host 127.0.0.1 --port 8016
```

- `pytest` covers the back end (feeds, subscriptions, retention, alerts, the
  worker's job handling, the API) against a temp `WREN_DATA`, with YouTube,
  downloads and push faked in `tests/conftest.py`. It never touches the network;
  keep it that way. Add tests with back-end changes.
- `WREN_WORKER=0` stops background checks and downloads; use a throwaway
  `WREN_DATA` (never the owner's data).
- `tests/test_e2e.py` drives the app in a browser (Playwright) against a live
  server with made-up data: each page renders at phone and desktop width
  without script errors, plus a few flows (tapping through, delete, subscribe,
  the desktop panes). It runs in WebKit where WebKit launches (it needs
  Ubuntu/Debian libraries), otherwise Chromium; `WREN_E2E_BROWSER` picks one.
  Keep it small: a smoke test, not a spec of every screen.
- Still check visual changes by eye at phone width (390×844) and desktop
  width, in light and dark. Headless screenshots:
  `chromium --headless=new --force-dark-mode --window-size=390,844
  --force-device-scale-factor=3 --virtual-time-budget=5000 --screenshot=out.png URL`.
- Screenshots for the README use made-up shows and generated artwork, never the
  owner's subscriptions.

## Deployment

- One container (see `Containerfile`), published to a container registry and
  tagged `latest` plus the short git SHA.
- On the server it runs as a systemd unit generated with `podman generate
  systemd --new` (so each start recreates the container from `:latest`), with
  `-p 8000:8000` and a persistent data directory mounted at `/data`, behind an
  HTTPS reverse proxy. A weekly timer restarts it so yt-dlp refreshes.
- Deploy steps: check `/api/overview` → `worker.activity` isn't `downloading`;
  back up the DB inside the container as `wren.db.bak-YYYYmmdd-HHMMSS` (sqlite
  backup API), keeping the newest five; pull the new image; restart the unit;
  verify the site. Don't `podman run`/`rm` by hand while the unit owns it.
- Host-specific details (hostnames, registry, paths) are kept out of the repo.
- Ask the owner before anything destructive: deleting server data, changing
  ports, volumes or the host setup.
- Git: commit finished work and push to `origin main`. Keep secrets, data and
  host details out of the repo.
