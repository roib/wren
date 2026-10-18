const $ = (id) => document.getElementById(id);
// Audio episodes play through <audio>, which iOS keeps playing in the background;
// video episodes use <video>. `media` is whichever one the current episode uses.
const videoEl = $("media");
const audioEl = $("audio");
let media = audioEl;

// Whether playback is *meant* to be running: set by the user (and the sleep
// timer), not by iOS pausing video when the app goes to the background.
let wantPlaying = false;

// Send a line to the server log (podman logs wren | grep client). sendBeacon
// still gets through while the app is being backgrounded.
function clientLog(msg) {
  try {
    navigator.sendBeacon("/api/client-log",
      new Blob([JSON.stringify({ msg })], { type: "application/json" }));
  } catch {}
}

// Listen on both elements, but only react to the one that's active, so a
// switch between them can't save or show the wrong episode's state.
function onMedia(type, handler) {
  for (const el of [audioEl, videoEl]) {
    el.addEventListener(type, (e) => { if (e.target === media) handler(e); });
  }
}
const SPEEDS = [1, 1.25, 1.5, 1.75, 2];
const MEDIA_CACHE = "wren-media-v1"; // must match sw.js
const DONE_AT = 0.97; // fraction of duration that counts as "played"
const RING = 2 * Math.PI * 8; // circumference of the download progress ring

// Cache API + service workers only exist on HTTPS (or localhost).
const canOffline = "caches" in window && "serviceWorker" in navigator;

let episodes = [];
let pending = []; // episodes on their way from subscriptions (from /api/pending)
let pendingWorker = null; // the server worker's status, to say when they'll arrive
let downloaded = new Set(); // media_url paths stored in MEDIA_CACHE
const downloading = new Map(); // episode key -> progress 0..1
const confirmingRemoval = new Set(); // episode keys showing "Remove?" after a first tap
let current = null;

// ---- icons ----------------------------------------------------------------

const ICONS = {
  play: '<path d="M8 5.6v12.8a1 1 0 0 0 1.5.86l10.2-6.4a1 1 0 0 0 0-1.72L9.5 4.74A1 1 0 0 0 8 5.6z" fill="currentColor" stroke="none"/>',
  pause: '<rect x="6.5" y="5" width="3.6" height="14" rx="1" fill="currentColor" stroke="none"/><rect x="13.9" y="5" width="3.6" height="14" rx="1" fill="currentColor" stroke="none"/>',
  back: '<path d="M4.6 12a7.4 7.4 0 1 0 2.2-5.25"/><path d="M4.6 4.4v3.9h3.9"/><text x="12.4" y="15.1" font-size="6.6" font-weight="700" text-anchor="middle" fill="currentColor" stroke="none" font-family="-apple-system,system-ui,sans-serif">15</text>',
  fwd: '<path d="M19.4 12a7.4 7.4 0 1 1-2.2-5.25"/><path d="M19.4 4.4v3.9h-3.9"/><text x="11.6" y="15.1" font-size="6.6" font-weight="700" text-anchor="middle" fill="currentColor" stroke="none" font-family="-apple-system,system-ui,sans-serif">30</text>',
  download: '<path d="M12 4.5v10.5M7.5 10.5 12 15l4.5-4.5M5.5 19.5h13"/>',
  downloaded: '<circle cx="12" cy="12" r="9" fill="currentColor" stroke="none"/><path d="M12 7.5v8M8.8 12.6 12 15.8l3.2-3.2" stroke="var(--bg)"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  settings: '<path d="M4 8h9M17 8h3M4 16h3M11 16h9"/><circle cx="15" cy="8" r="2"/><circle cx="9" cy="16" r="2"/>',
  chevronLeft: '<path d="M14.5 5.5 8 12l6.5 6.5"/>',
  chevronRight: '<path d="M9.5 5.5 16 12l-6.5 6.5"/>',
  chevronDown: '<path d="M5.5 9 12 15.5 18.5 9"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
  moon: '<path d="M19.5 14.6A7.8 7.8 0 1 1 9.4 4.5a6.3 6.3 0 0 0 10.1 10.1z"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  moonFill: '<path d="M19.5 14.6A7.8 7.8 0 1 1 9.4 4.5a6.3 6.3 0 0 0 10.1 10.1z" fill="currentColor"/>',
};

function svg(name) {
  return `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
}

function icon(name) {
  const span = document.createElement("span");
  span.className = "ic-wrap";
  span.innerHTML = svg(name);
  return span;
}

function ring(progress) {
  const span = document.createElement("span");
  span.className = "ic-wrap";
  span.innerHTML = `<svg class="ic ring" viewBox="0 0 24 24" aria-hidden="true">
    <circle cx="12" cy="12" r="8" class="ring-track"/>
    <circle cx="12" cy="12" r="8" class="ring-fill" stroke-dasharray="${RING}" stroke-dashoffset="${RING * (1 - progress)}"/>
  </svg>`;
  return span;
}

// ---- helpers --------------------------------------------------------------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (v !== null && v !== undefined && v !== false) el.setAttribute(k, v);
  }
  el.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

function fmt(sec) {
  if (!Number.isFinite(sec)) return "0:00";
  sec = Math.max(0, Math.floor(sec));
  const hh = Math.floor(sec / 3600);
  const mm = Math.floor((sec % 3600) / 60);
  const ss = String(sec % 60).padStart(2, "0");
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

function fmtMinutes(sec) {
  const m = Math.max(1, Math.round(sec / 60));
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

function fmtDate(yyyymmdd) {
  if (!yyyymmdd) return "";
  const d = new Date(`${yyyymmdd.slice(0, 4)}-${yyyymmdd.slice(4, 6)}-${yyyymmdd.slice(6, 8)}T00:00`);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

function fmtBytes(n) {
  if (n > 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  return `${Math.round(n / 1e6)} MB`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

let toastTimer;
function setStatus(text, ms = 3500) {
  const t = $("toast");
  t.textContent = text;
  t.classList.toggle("show", !!text);
  clearTimeout(toastTimer);
  if (text && ms) toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

// ---- per-episode state (localStorage) ---------------------------------------

const key = (ep) => `${ep.video_id}:${ep.mode}`;

function lsGet(k) {
  try { return localStorage.getItem(k); } catch { return null; }
}
function lsSet(k, v) {
  try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {}
}

const getPos = (ep) => Number(lsGet(`pos:${key(ep)}`)) || 0;
const setPos = (ep, t) => lsSet(`pos:${key(ep)}`, t > 0 ? String(Math.floor(t)) : null);
const isDone = (ep) => lsGet(`done:${key(ep)}`) === "1";

function setDone(ep, done) {
  lsSet(`done:${key(ep)}`, done ? "1" : null);
  setPos(ep, 0);
}

// "New" = arrived since you last opened that show. When a show page opens we
// remember the previous visit time for as long as you stay on it, so the
// markers don't vanish the moment you look at them.
const visitSnapshot = new Map(); // show name -> previous visit time
const seenKey = (name) => `seen:${name}`;

function seenSince() {
  // Before the first visit to a show, count from when this device first ran
  // the app, so an existing library doesn't all show up as new.
  let t = Number(lsGet("seenSince"));
  if (!t) {
    t = Math.floor(Date.now() / 1000);
    lsSet("seenSince", String(t));
  }
  return t;
}

function seenAt(name) {
  if (visitSnapshot.has(name)) return visitSnapshot.get(name);
  return Number(lsGet(seenKey(name))) || seenSince();
}

const isNew = (ep, showName) => !isDone(ep) && ep.added_at > seenAt(showName);

function markVisited(name) {
  if (visitSnapshot.has(name)) return;
  visitSnapshot.set(name, seenAt(name));
  lsSet(seenKey(name), String(Math.floor(Date.now() / 1000)));
}

// ---- data -----------------------------------------------------------------

async function loadLibrary(quiet = false) {
  try {
    const [res, pend] = await Promise.all([fetch("/api/library"), fetch("/api/pending")]);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    episodes = await res.json();
    lsSet("library", JSON.stringify(episodes)); // fallback for offline launches
    const p = pend.ok ? await pend.json() : { items: [], worker: null };
    pending = p.items;
    pendingWorker = p.worker;
  } catch {
    try { episodes = JSON.parse(lsGet("library")) || []; } catch { episodes = []; }
    pending = [];
    if (!quiet) setStatus("Offline — downloaded episodes still play", 5000);
  }
  await loadDownloaded();
  render();
  scheduleLibraryRefresh();
}

// Refresh the library every minute, or every 15 seconds while episodes are on
// their way, so you see them arrive.
let libraryTimer = null;
function scheduleLibraryRefresh() {
  clearTimeout(libraryTimer);
  libraryTimer = setTimeout(() => {
    if (document.visibilityState === "visible") loadLibrary(true);
    else scheduleLibraryRefresh();
  }, pending.length ? 15000 : 60000);
}

async function loadDownloaded() {
  if (!canOffline) return;
  const cache = await caches.open(MEDIA_CACHE);
  const keys = await cache.keys();
  downloaded = new Set(
    keys.map((r) => new URL(r.url).pathname).filter((p) => !p.endsWith(".jpg") && !p.endsWith(".vtt"))
  );
}

function groupShows(eps, coming = []) {
  const shows = new Map();
  const get = (name) => {
    if (!shows.has(name)) shows.set(name, { name, episodes: [], pending: [] });
    return shows.get(name);
  };
  for (const ep of eps) get(ep.show || ep.channel || "Unknown").episodes.push(ep);
  for (const item of coming) get(item.show).pending.push(item);

  const newest = (ep) => `${ep.upload_date || ""}${String(ep.added_at).padStart(12, "0")}`;
  for (const show of shows.values()) {
    show.episodes.sort((a, b) => newest(b).localeCompare(newest(a)));
    show.pending.reverse(); // newest first, like the episodes
  }
  // Shows you can play come first (most recent episode first); shows with
  // nothing downloaded yet follow, in name order.
  return [...shows.values()].sort((a, b) => {
    if (!a.episodes.length || !b.episodes.length) {
      return (b.episodes.length > 0) - (a.episodes.length > 0) || a.name.localeCompare(b.name);
    }
    return newest(b.episodes[0]).localeCompare(newest(a.episodes[0]));
  });
}

function fmtClock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// What's happening to an episode that hasn't arrived yet.
function pendingStatus(item) {
  const w = pendingWorker;
  if (item.state === "downloading") return "Downloading…";
  if (item.state === "premiere") return "Premieres soon";
  if (item.state === "retrying") {
    return /not a bot|Sign in to confirm|429|Too Many Requests/i.test(item.error)
      ? "Retrying · YouTube is blocking downloads" : "Retrying · download failed";
  }
  if (w?.paused_until) return `Queued · paused until ${fmtClock(w.paused_until)}`;
  const forced = w && (w.override_shows?.includes(item.show_id) || (w.override && !w.override_shows?.length));
  if (w && !w.active_now && !forced) return `Queued · downloads at ${fmtClock(w.resume_at)}`;
  return "Queued";
}

// ---- rendering --------------------------------------------------------------

const onSettings = () => location.hash.startsWith("#/settings");

function settingsTab() {
  if (location.hash === "#/settings/add") return "add";
  if (location.hash === "#/settings/downloads") return "downloads";
  return "subs";
}

function currentShow() {
  const m = location.hash.match(/^#\/show\/(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

function render() {
  const inSettings = onSettings();
  const showName = currentShow();
  const shows = groupShows(episodes, pending);
  const show = showName && shows.find((s) => s.name === showName);

  $("back").hidden = !(show || inSettings);
  $("settings-link").hidden = !!(show || inSettings);
  $("view-title").textContent = inSettings ? "Settings" : show ? show.name : "Shows";
  $("nav-title").textContent = $("view-title").textContent;
  $("list").hidden = inSettings;
  $("settings").hidden = !inSettings;

  let sub = "";
  if (show) {
    const unplayed = show.episodes.filter((ep) => !isDone(ep)).length;
    sub = [
      show.episodes.length ? plural(show.episodes.length, "episode") : null,
      unplayed ? `${unplayed} unplayed` : null,
      show.pending.length ? `${show.pending.length} coming` : null,
    ].filter(Boolean).join(" · ");
  }
  $("view-sub").textContent = sub;
  $("view-sub").hidden = !sub;

  if (inSettings) {
    renderSettings();
    return;
  }
  if (show) markVisited(show.name);

  const items = show
    ? [...show.pending.map(pendingRow), ...show.episodes.map((ep) => episodeRow(ep, show.name))]
    : shows.map(showRow);
  if (!items.length) {
    items.push(h("li", { class: "empty" },
      h("p", {}, "No episodes yet."),
      h("a", { href: "#/settings" }, "Add one in Settings")));
  }
  $("list").className = show ? "episodes" : "shows";
  $("list").replaceChildren(...items);
}

function showRow(show) {
  const unplayed = show.episodes.filter((ep) => !isDone(ep)).length;
  const fresh = show.episodes.filter((ep) => isNew(ep, show.name)).length;
  const coming = show.pending.length;
  const art = show.episodes.find((ep) => ep.thumb_url)?.thumb_url ?? show.pending[0]?.thumb_url;
  let sub = plural(show.episodes.length, "episode");
  if (!show.episodes.length) sub = `${plural(coming, "episode")} coming`;
  else if (fresh) sub = `${fresh} new`;
  else if (unplayed) sub = `${unplayed} unplayed`;
  if (show.episodes.length && coming) sub += ` · ${coming} coming`;

  return h("li", {},
    h("a", { class: `show${show.episodes.length ? "" : " coming"}`, href: `#/show/${encodeURIComponent(show.name)}` },
      h("img", { src: art ?? "", alt: "", loading: "lazy" }),
      h("span", { class: "show-text" },
        h("span", { class: "show-name" }, show.name),
        h("span", { class: "show-sub" }, fresh ? h("span", { class: "dot" }) : null, sub)),
      h("span", { class: "chev" }, icon("chevronRight"))));
}

function pendingRow(item) {
  const downloading = item.state === "downloading";
  return h("li", { class: `episode pending${downloading ? " downloading" : ""}`, "aria-disabled": "true" },
    h("div", { class: "ep-main" },
      h("span", { class: "ep-date" },
        downloading ? h("span", { class: "spinner", "aria-hidden": "true" }) : null,
        pendingStatus(item)),
      h("span", { class: "ep-title" }, item.title),
      h("span", { class: "ep-meta" }, item.duration ? fmtMinutes(item.duration) : "")));
}

function episodeRow(ep, showName) {
  const k = key(ep);
  const done = isDone(ep);
  const pos = getPos(ep);
  const isOffline = downloaded.has(ep.media_url);
  const unavailable = !navigator.onLine && !isOffline;
  const isCurrent = current && key(current) === k;
  const pct = !done && pos && ep.duration ? Math.min(100, (pos / ep.duration) * 100) : 0;

  let meta;
  if (done) meta = "Played";
  else if (pct) meta = `${fmtMinutes(ep.duration - pos)} left`;
  else meta = ep.duration ? fmtMinutes(ep.duration) : "";

  const classes = ["episode", done && "done", unavailable && "unavailable", isCurrent && "current"]
    .filter(Boolean).join(" ");

  return h("li", { class: classes },
    h("button", { class: "ep-main", onclick: () => play(ep) },
      h("span", { class: "ep-date" },
        isCurrent ? h("span", { class: "now" }, "Now playing · ") : null,
        !isCurrent && isNew(ep, showName) ? h("span", { class: "new" }, "New · ") : null,
        [fmtDate(ep.upload_date), ep.mode === "video" ? "Video" : null].filter(Boolean).join(" · ")),
      h("span", { class: "ep-title" }, ep.title),
      h("span", { class: "ep-meta" },
        pct ? h("span", { class: "bar" }, h("span", { style: `width:${pct}%` })) : null,
        meta)),
    h("span", { class: "ep-actions" },
      canOffline ? downloadButton(ep) : null,
      h("button", {
        class: `icon-btn sm check${done ? " on" : ""}`,
        "aria-label": done ? "Mark as unplayed" : "Mark as played",
        onclick: () => { setDone(ep, !done); render(); },
      }, icon("check"))));
}

function downloadButton(ep) {
  const k = key(ep);
  if (downloading.has(k)) {
    return h("button", { class: "icon-btn sm dl busy", "data-dl": k, "aria-label": "Downloading", disabled: true },
      ring(downloading.get(k)));
  }
  if (downloaded.has(ep.media_url)) {
    // Two taps to remove the copy on this device: the first asks "Remove?".
    if (confirmingRemoval.has(k)) {
      return h("button", {
        class: "remove-confirm", "aria-label": "Tap again to remove the download from this device",
        onclick: () => {
          confirmingRemoval.delete(k);
          removeDownload(ep);
        },
      }, "Remove?");
    }
    return h("button", {
      class: "icon-btn sm dl on", "aria-label": "Downloaded. Tap to remove from this device",
      onclick: () => {
        confirmingRemoval.add(k);
        render();
        setTimeout(() => { if (confirmingRemoval.delete(k)) render(); }, 3000);
      },
    }, icon("downloaded"));
  }
  return h("button", {
    class: "icon-btn sm dl", "aria-label": "Download for offline",
    onclick: () => download(ep),
  }, icon("download"));
}

function renderSettings() {
  const tab = settingsTab();
  document.querySelectorAll(".tabs a").forEach((a) => {
    const active = a.dataset.tab === tab;
    a.classList.toggle("active", active);
    a.setAttribute("aria-current", active ? "page" : "false");
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== tab;
  });
  renderSubscriptions();
  if (!canOffline) {
    $("dl-count").textContent = "Unavailable";
    $("dl-usage").textContent = "—";
    $("clear-area").replaceChildren();
    $("dl-hint").textContent = "Offline downloads need HTTPS, e.g. via tailscale serve.";
    return;
  }
  const clearable = episodes.filter((ep) => isDone(ep) && downloaded.has(ep.media_url));
  $("dl-count").textContent = plural(downloaded.size, "episode");
  $("clear-area").replaceChildren(
    h("button", {
      class: "row-btn",
      disabled: !clearable.length,
      onclick: () => clearDownloads(clearable),
    }, clearable.length
      ? `Clear ${plural(clearable.length, "played download")}`
      : "No played downloads to clear"));
  navigator.storage?.estimate?.().then(({ usage }) => {
    $("dl-usage").textContent = fmtBytes(usage);
  });
}

// ---- subscriptions -----------------------------------------------------------

let subs = []; // from /api/shows; null when the server can't be reached
let queueInfo = null; // from /api/queue
let subsTimer = null;
const confirming = new Set(); // show ids waiting for a second tap on Remove
let expanded = null; // id of the subscription row that's open, showing its actions
let showSubForm = false; // the subscribe form, behind "+ Add" once you have subscriptions

function ago(ts) {
  if (!ts) return "never";
  const m = Math.floor((Date.now() / 1000 - ts) / 60);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  if (m < 1440) return `${Math.floor(m / 60)}h ago`;
  return `${Math.floor(m / 1440)}d ago`;
}

function until(ts) {
  const m = Math.round((ts - Date.now() / 1000) / 60);
  if (m <= 0) return "soon";
  return m < 60 ? `in ${m} min` : `in ${Math.floor(m / 60)}h ${m % 60}m`;
}

async function loadSubscriptions() {
  try {
    const [s, q] = await Promise.all([fetch("/api/shows"), fetch("/api/queue")]);
    if (!s.ok || !q.ok) throw new Error();
    subs = await s.json();
    queueInfo = await q.json();
  } catch {
    subs = null;
    queueInfo = null;
  }
  renderSubscriptions();
  scheduleSubscriptionsRefresh();
}

function dueCount() {
  if (!queueInfo) return 0;
  const now = Date.now() / 1000;
  return queueInfo.jobs.length + queueInfo.items.filter((i) => i.next_try <= now + 5).length;
}

// Poll quickly while the server is busy so progress shows up; slowly otherwise.
function scheduleSubscriptionsRefresh() {
  clearTimeout(subsTimer);
  if (!onSettings()) return;
  const busy = queueInfo && (queueInfo.worker.activity !== "idle" || dueCount() > 0);
  subsTimer = setTimeout(() => {
    loadSubscriptions();
    if (busy) loadLibrary(true);
  }, busy ? 5000 : 30000);
}

// Downloads that failed and are waiting to retry (premieres not yet aired don't count).
function retrying(showId) {
  if (!queueInfo) return [];
  return queueInfo.items.filter((i) => i.error && i.error !== "upcoming"
    && (showId === undefined || i.show_id === showId));
}

function friendlyError(err) {
  if (/not a bot|Sign in to confirm/i.test(err)) return "YouTube wants to confirm the server isn't a bot (set WREN_COOKIES)";
  if (/429|Too Many Requests/.test(err)) return "YouTube is rate-limiting the server";
  if (/unavailable|private|removed/i.test(err)) return "video unavailable";
  return err.replace(/^ERROR:\s*(\[\w+\]\s*[\w-]+:\s*)?/, "").slice(0, 120);
}

function workerStatus() {
  if (!queueInfo) return subs === null ? "Can't reach the server right now." : "";
  const w = queueInfo.worker;
  const due = dueCount();
  let text = "Up to date";
  if (w.paused_until) text = `YouTube is rate-limiting the server · paused until ${fmtClock(w.paused_until)}`;
  else if (w.activity === "checking shows") text = "Checking for new episodes…";
  else if (due && w.override) text = `Downloading now · ${plural(due, "episode")} to go`;
  else if (due && !w.active_now) text = `${plural(due, "download")} waiting until ${fmtClock(w.resume_at)}`;
  else if (due) text = `Downloading · ${plural(due, "episode")} to go`;
  else if (w.activity === "downloading") text = "Downloading…";
  else if (retrying().length) text = `Waiting to retry ${plural(retrying().length, "download")}`;
  return subs && subs.length ? `${text} · next check ${until(w.next_poll)}` : text;
}

const FILTER_AFTER = 6; // show the filter box once there are more subscriptions than this

function renderSubscriptions() {
  const have = !!(subs && subs.length);
  const none = !!(subs && !subs.length);

  const formOpen = showSubForm || none;
  $("sub-form").hidden = !formOpen;
  $("sub-empty").hidden = !none;
  const toggle = $("sub-add-toggle");
  toggle.hidden = !have;
  toggle.textContent = showSubForm ? "Cancel" : "+ Add";
  toggle.setAttribute("aria-expanded", String(formOpen));

  const filterEl = $("sub-filter");
  filterEl.hidden = !(subs && subs.length > FILTER_AFTER);
  $("sub-count").hidden = !filterEl.hidden;
  $("sub-count").textContent = have ? plural(subs.length, "subscription") : "";

  const status = workerStatus();
  $("sub-status").textContent = status;
  $("download-all").hidden = !have;
  $("sub-statusbar").hidden = !status && !have;

  const list = $("sub-list");
  list.hidden = !have;
  if (!have) return;
  const q = filterEl.hidden ? "" : filterEl.value.trim().toLowerCase();
  const shown = q ? subs.filter((s) => s.name.toLowerCase().includes(q)) : subs;
  list.replaceChildren(...(shown.length
    ? shown.map(subscriptionRow)
    : [h("p", { class: "sub-none" }, "No subscriptions match.")]));
}

function subscriptionArt(s) {
  return episodes.find((ep) => ep.show_id === s.id && ep.thumb_url)?.thumb_url;
}

function subscriptionRow(s) {
  const open = expanded === s.id;
  const waiting = retrying(s.id);
  let warn = null;
  if (s.last_error) warn = friendlyError(s.last_error);
  else if (waiting.length) warn = `Retrying ${waiting.length}: ${friendlyError(waiting[0].error)}`;
  else if (s.failed) warn = `${plural(s.failed, "download")} failed`;

  const status = [
    s.queued ? `${s.queued} queued` : plural(s.episodes, "episode"),
    `checked ${ago(s.last_checked)}`,
  ].join(" · ");
  const detail = [
    `${plural(s.episodes, "episode")} downloaded`,
    s.keep_last > 0 ? `keeps the latest ${s.keep_last}` : "keeps everything",
    s.mode,
  ].join(" · ");
  const art = subscriptionArt(s);
  const busy = queueInfo?.worker.override_shows?.includes(s.id);
  const isConfirming = confirming.has(s.id);

  return h("div", { class: `sub-row${open ? " open" : ""}` },
    h("button", {
      class: "sub-head",
      "aria-expanded": String(open),
      // Toggle classes in place (no re-render) so the row animates open/closed.
      onclick: (e) => {
        const row = e.currentTarget.closest(".sub-row");
        const wasOpen = row.classList.contains("open");
        for (const r of $("sub-list").querySelectorAll(".sub-row.open")) {
          r.classList.remove("open");
          r.querySelector(".sub-head").setAttribute("aria-expanded", "false");
        }
        if (!wasOpen) {
          row.classList.add("open");
          e.currentTarget.setAttribute("aria-expanded", "true");
        }
        expanded = wasOpen ? null : s.id;
      },
    },
      art
        ? h("img", { class: "sub-art", src: art, alt: "", loading: "lazy" })
        : h("span", { class: "sub-art letter", "aria-hidden": "true" }, s.name.slice(0, 1).toUpperCase()),
      h("span", { class: "sub-text" },
        h("span", { class: "sub-name" }, s.name),
        h("span", { class: "sub-meta" }, status),
        warn ? h("span", { class: "sub-warn" }, warn) : null),
      h("span", { class: "chev" }, icon("chevronDown"))),
    // Always rendered; collapsed with CSS until the row is open.
    h("div", { class: "sub-collapse" },
      h("div", { class: "sub-body" },
        h("div", { class: "sub-body-inner" },
          h("p", { class: "sub-detail" }, detail),
          h("div", { class: "sub-buttons" },
            h("button", {
              class: "action",
              disabled: busy,
              tabindex: open ? null : "-1",
              onclick: () => downloadShowNow(s),
            }, icon("download"), busy ? "Downloading…" : "Download now"),
            h("button", {
              class: `action subtle${isConfirming ? " confirm" : ""}`,
              tabindex: open ? null : "-1",
              onclick: () => removeShow(s),
            }, isConfirming ? "Tap again to remove" : "Remove"))))));
}

// Check one show and download its queue now, even outside the server's active hours.
async function downloadShowNow(s) {
  try {
    const res = await fetch(`/api/shows/${s.id}/download-now`, { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    setStatus(`Checking ${s.name} and downloading now…`);
  } catch (err) {
    setStatus(`Couldn't start: ${err.message}`);
  }
  setTimeout(loadSubscriptions, 2500);
}

// Check every show and download what's queued now, even outside active hours.
async function downloadNow() {
  try {
    await fetch("/api/download-now", { method: "POST" });
    setStatus("Checking shows and downloading now…");
  } catch {
    setStatus("Can't reach the server right now.");
  }
  setTimeout(loadSubscriptions, 3000);
}

$("download-all").addEventListener("click", downloadNow);
$("sub-filter").addEventListener("input", renderSubscriptions);
$("sub-add-toggle").addEventListener("click", () => {
  showSubForm = !showSubForm;
  renderSubscriptions();
  if (showSubForm) $("sub-url").focus();
});

async function removeShow(s) {
  if (!confirming.has(s.id)) {
    confirming.add(s.id);
    renderSubscriptions();
    setTimeout(() => {
      confirming.delete(s.id);
      renderSubscriptions();
    }, 3000);
    return;
  }
  confirming.delete(s.id);
  if (expanded === s.id) expanded = null;
  try {
    const res = await fetch(`/api/shows/${s.id}`, { method: "DELETE" });
    if (!res.ok && res.status !== 404) throw new Error(`HTTP ${res.status}`);
    setStatus(`Unsubscribed from ${s.name}. Its episodes stay in your library.`, 5000);
  } catch (err) {
    setStatus(`Couldn't unsubscribe: ${err.message}`);
  }
  loadSubscriptions();
  loadLibrary(true);
}

$("sub-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("sub-submit");
  const url = $("sub-url").value.trim();
  const mode = new FormData(e.target).get("sub-mode");
  btn.disabled = true;
  btn.textContent = "Looking up show…";
  try {
    const res = await fetch("/api/shows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, mode }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    $("sub-url").value = "";
    showSubForm = false;
    setStatus(`Subscribed to ${data.name}. Fetching the latest episodes…`, 5000);
    loadSubscriptions();
  } catch (err) {
    setStatus(`Couldn't subscribe: ${err.message}`, 8000);
  } finally {
    btn.disabled = false;
    btn.textContent = "Subscribe";
  }
});

// ---- offline downloads ----------------------------------------------------

async function download(ep) {
  const k = key(ep);
  if (downloading.has(k)) return;
  downloading.set(k, 0);
  render();
  try {
    // Ask the browser not to evict our downloads under storage pressure.
    navigator.storage?.persist?.();

    const res = await fetch(ep.media_url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const total = Number(res.headers.get("Content-Length")) || 0;

    // Count bytes as they stream into the cache, without buffering in memory.
    let loaded = 0;
    let lastUi = 0;
    const counter = new TransformStream({
      transform(chunk, ctl) {
        loaded += chunk.byteLength;
        if (total && Date.now() - lastUi > 200) {
          lastUi = Date.now();
          downloading.set(k, loaded / total);
          const fill = document.querySelector(`[data-dl="${CSS.escape(k)}"] .ring-fill`);
          if (fill) fill.setAttribute("stroke-dashoffset", RING * (1 - loaded / total));
        }
        ctl.enqueue(chunk);
      },
    });

    const cache = await caches.open(MEDIA_CACHE);
    await cache.put(ep.media_url, new Response(res.body.pipeThrough(counter), {
      headers: { "Content-Type": res.headers.get("Content-Type") || "application/octet-stream" },
    }));
    if (ep.thumb_url) await cache.add(ep.thumb_url).catch(() => {});
    if (ep.subs_url) await cache.add(ep.subs_url).catch(() => {});
    downloaded.add(ep.media_url);
  } catch (e) {
    setStatus(`Download failed: ${e.message}`);
  } finally {
    downloading.delete(k);
    render();
  }
}

async function removeDownload(ep) {
  await clearDownloads([ep]);
}

// Removes only the offline copies; episodes stay in the library and can be re-downloaded.
async function clearDownloads(eps) {
  const cache = await caches.open(MEDIA_CACHE);
  let n = 0;
  for (const ep of eps) {
    if (current && key(current) === key(ep) && !media.paused) continue; // don't yank what's playing
    await cache.delete(ep.media_url);
    if (ep.subs_url) await cache.delete(ep.subs_url);
    downloaded.delete(ep.media_url);
    n++;
  }
  if (eps.length > 1) setStatus(`Cleared ${plural(n, "download")}`);
  render();
}

// ---- player ---------------------------------------------------------------

function play(ep) {
  if (current && key(current) === key(ep)) {
    openSheet();
    if (media.paused) media.play().catch(() => {});
    return;
  }
  if (isDone(ep)) setDone(ep, false); // replaying a finished episode starts it over
  if (current && !isDone(current)) setPos(current, media.currentTime); // remember where we left off
  current = ep;

  const isVideo = ep.mode === "video";
  const next = isVideo ? videoEl : audioEl;
  handoff = false;
  wantPlaying = true;
  videoEl.classList.remove("stale");
  hideOverlay();
  if (next !== media) {
    // Stop and unload the other element before switching.
    media.pause();
    media.removeAttribute("src");
    media.load();
    media = next;
  }
  $("mini").hidden = false;
  document.body.classList.add("has-player");
  $("sheet").classList.toggle("is-video", isVideo);
  $("mini-title").textContent = ep.title;
  $("mini-sub").textContent = ep.channel ?? "";
  $("mini-thumb").src = ep.thumb_url ?? "";
  $("sheet-title").textContent = ep.title;
  $("sheet-channel").textContent = ep.channel ?? "";
  $("sheet-art").src = ep.thumb_url ?? "";
  if (isVideo) videoEl.poster = ep.thumb_url ?? "";
  media.src = ep.media_url;
  if (isVideo) setupSubtitles(ep);
  else $("cc").hidden = true;
  media.playbackRate = Number(lsGet("speed")) || 1;

  const resumeAt = getPos(ep);
  media.addEventListener("loadedmetadata", () => {
    if (resumeAt && resumeAt < media.duration - 5) media.currentTime = resumeAt;
  }, { once: true });

  media.play().catch(() => {}); // autoplay may be blocked until user gesture
  if (isVideo) unlockAudio(ep);
  setupMediaSession(ep);
  if (isVideo) openSheet();
  render();
}

// ---- background audio for video episodes ------------------------------------
// iOS pauses <video> when the app goes to the background. For video episodes,
// hand playback to the <audio> element (same file) on the way out, and back to
// the video on return. The audio element is "unlocked" by the tap that started
// the episode, so iOS lets it start later without another tap.
let handoff = false; // true while <audio> is standing in for a video episode

function unlockAudio(ep) {
  audioEl.src = ep.media_url;
  audioEl.preload = "metadata"; // don't start fetching the big video file twice
  audioEl.muted = true;
  audioEl.play().catch(() => {}); // within the tap: clears iOS's play restriction
  audioEl.pause();
  audioEl.muted = false;
}

// When iOS last paused the video on its own, to measure how early it does that.
let videoPausedAt = 0;
videoEl.addEventListener("pause", () => { videoPausedAt = performance.now(); });

function handOffToAudio() {
  if (handoff || media !== videoEl || !current || current.mode !== "video" || !wantPlaying) return;
  handbackToken++; // cancel any hand-back still waiting
  const t = videoEl.currentTime;
  const rate = videoEl.playbackRate;
  if (!sameSrc(audioEl, current.media_url)) audioEl.src = current.media_url;
  media = audioEl;
  handoff = true;
  videoEl.pause();
  // Hide the frozen frame; it fades back in once the video plays from the new spot.
  videoEl.classList.add("stale");
  const t0 = performance.now();
  const start = () => {
    if (Math.abs(audioEl.currentTime - t) > 0.25) audioEl.currentTime = t;
    audioEl.playbackRate = rate;
    audioEl.addEventListener("playing", () => {
      clientLog(`handoff: video -> audio at ${Math.round(t)}s, sound after ${Math.round(performance.now() - t0)}ms`);
    }, { once: true });
    audioEl.play().catch((err) => {
      clientLog(`handoff blocked at ${Math.round(t)}s: ${err.name} ${err.message}`);
      media = videoEl;
      handoff = false;
      videoEl.classList.remove("stale");
    });
  };
  if (audioEl.readyState >= 1) start();
  else audioEl.addEventListener("loadedmetadata", start, { once: true });
}

// Hand back without a gap: keep the audio playing while the video gets going a
// little ahead (muted, so iOS actually loads it; it won't buffer a paused
// video), hold it there until the audio catches up, then switch.
let handbackToken = 0;
const HANDBACK_GIVE_UP_MS = 5000;

function handBackToVideo() {
  if (!handoff) return;
  const token = ++handbackToken;
  const live = () => token === handbackToken && handoff;
  if (audioEl.paused) {
    finishHandback(audioEl.currentTime, false);
    return;
  }
  // Never leave the video black: if it's too slow, switch anyway.
  const giveUp = setTimeout(() => {
    if (!live()) return;
    clientLog("handback: video slow to load, switching anyway");
    finishHandback(audioEl.currentTime, !audioEl.paused);
  }, HANDBACK_GIVE_UP_MS);

  const attempt = (lead) => {
    if (!live()) return;
    videoEl.muted = true;
    videoEl.playbackRate = audioEl.playbackRate;
    videoEl.currentTime = audioEl.currentTime + lead * audioEl.playbackRate;
    videoEl.play().catch(() => {});
    whenPlayable(videoEl, () => {
      if (!live()) return;
      const ahead = (videoEl.currentTime - audioEl.currentTime) / audioEl.playbackRate;
      if (ahead < 0.1) {
        // Loading took longer than the lead: cue further ahead and try again.
        if (lead >= 8) { clearTimeout(giveUp); finishHandback(audioEl.currentTime, true); }
        else attempt(lead * 2);
        return;
      }
      videoEl.pause(); // hold the loaded frame until the audio gets there
      setTimeout(() => {
        if (!live()) return;
        clearTimeout(giveUp);
        finishHandback(videoEl.currentTime, true, true);
      }, ahead * 1000);
    });
  };
  attempt(1);
}

// Calls fn once the element has finished seeking and has data to play.
function whenPlayable(el, fn) {
  const ready = () => !el.seeking && el.readyState >= 3;
  if (ready()) { fn(); return; }
  const check = () => {
    if (!ready()) return;
    for (const type of ["playing", "canplay", "seeked"]) el.removeEventListener(type, check);
    fn();
  };
  for (const type of ["playing", "canplay", "seeked"]) el.addEventListener(type, check);
}

function finishHandback(t, play, alreadyCued = false) {
  media = videoEl; // switch first, so the audio's pause event is ignored
  handoff = false;
  handbackToken++;
  videoEl.muted = false;
  if (!alreadyCued) videoEl.currentTime = t;
  let shown = false;
  const reveal = () => {
    if (shown) return;
    shown = true;
    videoEl.classList.remove("stale");
  };
  setTimeout(reveal, 1500); // whatever happens, don't stay black
  if (play) {
    videoEl.addEventListener("playing", reveal, { once: true });
    videoEl.play().catch((err) => { clientLog(`handback play failed: ${err.name}`); reveal(); });
  } else if (videoEl.seeking) {
    videoEl.addEventListener("seeked", reveal, { once: true });
  } else {
    reveal();
  }
  audioEl.pause();
  clientLog(`handback: audio -> video at ${Math.round(t)}s (${play ? (alreadyCued ? "seamless" : "re-cued") : "paused"})`);
  syncPlayButtons();
  syncTime();
}

// While a video plays on screen, keep the paused audio element cued near the
// same spot, so a hand-off has data ready instead of starting cold.
setInterval(() => {
  if (handoff || media !== videoEl || videoEl.paused || document.visibilityState !== "visible") return;
  if (!current || current.mode !== "video" || !sameSrc(audioEl, current.media_url)) return;
  audioEl.currentTime = videoEl.currentTime + 1;
}, 10000);

// ---- subtitles (video episodes) --------------------------------------------
const subsOn = () => lsGet("subs") === "on";

function setupSubtitles(ep) {
  videoEl.querySelectorAll("track").forEach((t) => t.remove());
  $("cc").hidden = !ep.subs_url;
  if (!ep.subs_url) return;
  const track = document.createElement("track");
  track.kind = "subtitles";
  track.srclang = "en";
  track.label = "English";
  track.src = ep.subs_url;
  // YouTube's automatic captions pin every line to the left edge
  // (align:start position:0%); center them along the bottom instead.
  track.addEventListener("load", () => centerCues(track.track));
  videoEl.append(track);
  applySubtitles();
}

function centerCues(textTrack) {
  for (const cue of textTrack.cues || []) {
    cue.align = "center";
    cue.position = "auto";
    cue.size = 100;
    cue.line = "auto";
  }
}

function applySubtitles() {
  const on = subsOn();
  for (const t of videoEl.textTracks) t.mode = on ? "showing" : "hidden";
  $("cc").setAttribute("aria-pressed", String(on));
  $("cc").setAttribute("aria-label", on ? "Hide subtitles" : "Show subtitles");
}

$("cc").addEventListener("click", () => {
  lsSet("subs", subsOn() ? "off" : "on");
  applySubtitles();
});

function openSheet() {
  const el = $("sheet");
  if (el.classList.contains("open")) return;
  el.setAttribute("aria-hidden", "false");
  // Safari skips the slide-up if the sheet goes from hidden to open in one step:
  // make it renderable (still off-screen) first, then open it a frame later.
  el.style.visibility = "visible";
  el.getBoundingClientRect();
  requestAnimationFrame(() => requestAnimationFrame(() => {
    el.classList.add("open");
    el.style.visibility = "";
  }));
}

function closeSheet() {
  closeSleepMenu();
  $("sheet").classList.remove("open");
  $("sheet").setAttribute("aria-hidden", "true");
}

// ---- swipe down to close the player ------------------------------------------

let drag = null;
const sheetEl = $("sheet");

sheetEl.addEventListener("touchstart", (e) => {
  // Leave the scrubber, the video's own controls and the sleep menu alone.
  if (e.touches.length !== 1 || e.target.closest("input, .popover, #sleep-backdrop, .scrubber")) return;
  drag = { y: e.touches[0].clientY, t: Date.now(), dy: 0 };
}, { passive: true });

sheetEl.addEventListener("touchmove", (e) => {
  if (!drag) return;
  drag.dy = Math.max(0, e.touches[0].clientY - drag.y);
  sheetEl.style.transition = "none";
  sheetEl.style.transform = `translateY(${drag.dy}px)`;
}, { passive: true });

function endDrag() {
  if (!drag) return;
  const fast = drag.dy / Math.max(1, Date.now() - drag.t) > 0.6;
  const close = drag.dy > window.innerHeight / 3 || (fast && drag.dy > 40);
  // Clearing the inline styles animates from where the finger let go, either
  // back up or off the bottom.
  sheetEl.style.transition = "";
  sheetEl.style.transform = "";
  if (close) closeSheet();
  drag = null;
}
sheetEl.addEventListener("touchend", endDrag);
sheetEl.addEventListener("touchcancel", endDrag);

// ---- sleep timer --------------------------------------------------------------

const SLEEP_OPTIONS = [
  { label: "When episode ends", value: "end" },
  { label: "1 hour", value: 60 },
  { label: "45 minutes", value: 45 },
  { label: "30 minutes", value: 30 },
  { label: "15 minutes", value: 15 },
  { label: "10 minutes", value: 10 },
  { label: "5 minutes", value: 5 },
  { label: "Off", value: 0 },
];
let sleepValue = 0; // 0 (off), minutes, or "end"
let sleepAt = null; // wall-clock deadline for timed choices

// Seconds until the sleep timer pauses playback, or null when it's off.
function sleepSecondsLeft(episodeLeft) {
  if (sleepValue === "end") return Math.max(0, episodeLeft || 0);
  if (sleepAt) return Math.max(0, Math.ceil((sleepAt - Date.now()) / 1000));
  return null;
}

function syncSleep() {
  const btn = $("sleep");
  btn.innerHTML = svg(sleepValue ? "moonFill" : "moon");
  btn.classList.toggle("on", sleepValue !== 0);
  const left = sleepAt ? Math.max(1, Math.ceil((sleepAt - Date.now()) / 60000)) : 0;
  btn.setAttribute("aria-label", sleepValue === 0 ? "Sleep timer: off"
    : sleepValue === "end" ? "Sleep timer: end of episode" : `Sleep timer: ${left} min left`);
}

function setSleep(value) {
  sleepValue = value;
  sleepAt = typeof value === "number" && value ? Date.now() + value * 60000 : null;
  syncSleep();
  syncTime();
}

function checkSleep() {
  if (sleepAt && Date.now() >= sleepAt) {
    wantPlaying = false;
    media.pause();
    setSleep(0);
    setStatus("Sleep timer ended");
  }
}

function openSleepMenu() {
  $("sleep-menu").replaceChildren(...SLEEP_OPTIONS.map((opt) => {
    const checked = opt.value === sleepValue;
    return h("button", {
      class: "popover-item",
      role: "menuitemradio",
      "aria-checked": String(checked),
      onclick: () => {
        setSleep(opt.value);
        closeSleepMenu();
      },
    }, h("span", { class: "popover-check" }, checked ? icon("check") : null), opt.label);
  }));
  $("sleep-menu").hidden = false;
  $("sleep-backdrop").hidden = false;
  $("sleep").setAttribute("aria-expanded", "true");
}

function closeSleepMenu() {
  $("sleep-menu").hidden = true;
  $("sleep-backdrop").hidden = true;
  $("sleep").setAttribute("aria-expanded", "false");
}

$("sleep").addEventListener("click", () => {
  $("sleep-menu").hidden ? openSleepMenu() : closeSleepMenu();
});
$("sleep-backdrop").addEventListener("click", closeSleepMenu);

// timeupdate keeps firing while audio plays in the background, unlike timers
// that iOS may slow down, so check the deadline there too.
// Tick the countdown every second while a timer is on (it runs even when paused).
setInterval(() => {
  if (!sleepValue) return;
  checkSleep();
  syncTime();
  syncSleep();
}, 1000);

function markCurrentDone() {
  if (!current || isDone(current)) return;
  setDone(current, true);
  render();
}

function setupMediaSession(ep) {
  if (!("mediaSession" in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: ep.title,
    artist: ep.channel ?? "",
    artwork: ep.thumb_url ? [{ src: ep.thumb_url, sizes: "1280x720", type: "image/jpeg" }] : [],
  });
  const ms = navigator.mediaSession;
  ms.setActionHandler("play", resumeFromSystem);
  ms.setActionHandler("pause", pauseFromSystem);
  ms.setActionHandler("seekbackward", (d) => skip(-(d.seekOffset || 15)));
  ms.setActionHandler("seekforward", (d) => skip(d.seekOffset || 30));
  ms.setActionHandler("seekto", (d) => { media.currentTime = d.seekTime; });
}

if (navigator.audioSession) {
  try { navigator.audioSession.type = "playback"; } catch {}
}

// Lock screen / Control Center. Resuming after a pause in the background only
// works for a few seconds: iOS then drops a home-screen app's audio session and
// the play button can't reach it until the app is opened (WebKit bug 243258).
// A silent <audio> keepalive took over Now Playing, and Web Audio is blocked in
// the background, so there's no web-side workaround.
function pauseFromSystem() {
  wantPlaying = false;
  media.pause();
  if (current && !isDone(current)) setPos(current, media.currentTime); // where to resume
}

function resumeFromSystem() {
  wantPlaying = true;
  media.play().catch((err) => clientLog(`resume (lock screen) failed: ${err.name}`));
}

// An element's source without any media fragment (#t=...).
function srcOf(el) {
  return (el.currentSrc || el.src).split("#")[0];
}

function sameSrc(el, url) {
  return srcOf(el).endsWith(url);
}

function skip(delta) {
  media.currentTime = Math.max(0, Math.min(media.duration || 0, media.currentTime + delta));
}

function togglePlay() {
  if (media.paused) {
    wantPlaying = true;
    media.play().catch(() => {});
  } else {
    wantPlaying = false;
    media.pause();
  }
}

function syncPlayButtons() {
  const name = media.paused ? "play" : "pause";
  const label = media.paused ? "Play" : "Pause";
  for (const id of ["play", "mini-play", "ov-play"]) {
    $(id).innerHTML = svg(name);
    $(id).setAttribute("aria-label", label);
  }
}

function syncSpeed() {
  $("speed").textContent = `${media.playbackRate}×`;
}

function syncTime() {
  // While the finger is on the scrubber, it owns the position; don't fight it.
  if (!scrubbing) showTime(media.currentTime || 0);
}

function showTime(t) {
  const d = media.duration || 0;
  const pct = d ? (t / d) * 100 : 0;
  $("seek").style.setProperty("--pct", `${pct}%`);
  $("seek").setAttribute("aria-valuenow", String(Math.floor(t)));
  $("seek").setAttribute("aria-valuetext", fmt(t));
  $("mini-bar").style.width = `${pct}%`;
  $("cur").textContent = fmt(t);
  // With a sleep timer on, the right-hand time counts down to sleep instead
  // (like Apple Podcasts): "ᶻz 4:51".
  const left = sleepSecondsLeft(d - t);
  $("remain").textContent = left === null ? `-${fmt(d - t)}` : `ᶻz ${fmt(left)}`;
  $("remain").classList.toggle("sleeping", left !== null);
}

$("play").addEventListener("click", togglePlay);
// ---- video overlay ------------------------------------------------------------
// Tapping the picture shows play/pause, CC and full screen; while playing it
// fades out after a few seconds. Tapping the dim area (not a button) hides it.
const OVERLAY_HIDE_MS = 3000;
let overlayTimer = null;

function showOverlay() {
  $("video-overlay").classList.add("show");
  scheduleOverlayHide();
}

function hideOverlay() {
  clearTimeout(overlayTimer);
  $("video-overlay").classList.remove("show");
}

function scheduleOverlayHide() {
  clearTimeout(overlayTimer);
  // Stays up while paused, so it's clear how to resume.
  if (!videoEl.paused) overlayTimer = setTimeout(hideOverlay, OVERLAY_HIDE_MS);
}

// Taps on the picture: single tap shows/hides the overlay; double-tap on the
// left or right third skips back 15 s / forward 30 s (keep tapping to keep
// skipping). Side taps wait a moment to tell a single tap from a double.
const DOUBLE_TAP_MS = 300;
let lastPictureTap = { at: 0, side: null };
let singleTapTimer = null;
let seekHint = { total: 0, timer: null };

$("video-wrap").addEventListener("click", (e) => {
  if (e.target.closest("button")) {
    scheduleOverlayHide(); // an overlay button was used: keep it up a little longer
    return;
  }
  const r = $("video-wrap").getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width;
  const side = x < 0.35 ? "back" : x > 0.65 ? "fwd" : null;
  const now = performance.now();
  if (side && side === lastPictureTap.side && now - lastPictureTap.at < DOUBLE_TAP_MS) {
    clearTimeout(singleTapTimer);
    skip(side === "back" ? -15 : 30);
    showSeekHint(side);
    lastPictureTap = { at: now, side }; // chain further taps
    return;
  }
  lastPictureTap = { at: now, side };
  const wasShown = $("video-overlay").classList.contains("show");
  clearTimeout(singleTapTimer);
  singleTapTimer = setTimeout(() => (wasShown ? hideOverlay() : showOverlay()),
    side ? DOUBLE_TAP_MS : 0);
});

function showSeekHint(side) {
  const el = $("seek-hint");
  if (!el.classList.contains(side)) seekHint.total = 0;
  seekHint.total += side === "back" ? 15 : 30;
  el.className = `seek-hint ${side} show`;
  el.textContent = `${side === "back" ? "−" : "+"}${seekHint.total}`;
  clearTimeout(seekHint.timer);
  seekHint.timer = setTimeout(() => {
    el.classList.remove("show");
    seekHint.total = 0;
  }, 700);
}
$("ov-play").addEventListener("click", togglePlay);
// Full screen: iPhones only allow it through iOS's own video player
// (webkitEnterFullscreen); other browsers use the standard API.
$("fullscreen").addEventListener("click", () => {
  if (!document.fullscreenEnabled && videoEl.webkitEnterFullscreen) videoEl.webkitEnterFullscreen();
  else if (videoEl.requestFullscreen) videoEl.requestFullscreen().catch(() => {});
  else if (videoEl.webkitEnterFullscreen) videoEl.webkitEnterFullscreen();
});
$("mini-play").addEventListener("click", togglePlay);
$("mini-open").addEventListener("click", openSheet);
$("sheet-close").addEventListener("click", closeSheet);
$("skip-back").addEventListener("click", () => skip(-15));
$("skip-fwd").addEventListener("click", () => skip(30));
$("speed").addEventListener("click", () => {
  const next = SPEEDS[(SPEEDS.indexOf(media.playbackRate) + 1) % SPEEDS.length];
  media.playbackRate = next;
  lsSet("speed", String(next));
});
// ---- scrubber ---------------------------------------------------------------
// A custom slider instead of <input type=range>: on iOS the native one only
// moves if you start exactly on its small knob, and seeking on every move makes
// it stutter. Here a touch anywhere on the (tall, invisible) hit area jumps to
// that point and follows the finger; the audio seeks once, on release.
let scrubbing = false;
let scrubTime = 0;

function scrubTo(clientX) {
  const r = $("seek").getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
  scrubTime = frac * (media.duration || 0);
  showTime(scrubTime);
}

$("seek").addEventListener("pointerdown", (e) => {
  if (!media.duration || (e.pointerType === "mouse" && e.button !== 0)) return;
  e.preventDefault();
  scrubbing = true;
  $("seek").classList.add("dragging");
  try { $("seek").setPointerCapture(e.pointerId); } catch {} // keep following off the bar
  scrubTo(e.clientX);
});
$("seek").addEventListener("pointermove", (e) => {
  if (scrubbing) scrubTo(e.clientX);
});
$("seek").addEventListener("pointerup", () => {
  if (!scrubbing) return;
  scrubbing = false;
  $("seek").classList.remove("dragging");
  media.currentTime = scrubTime;
  showTime(scrubTime);
});
$("seek").addEventListener("pointercancel", () => {
  // The gesture was taken away (e.g. by the system): put it back where it was.
  scrubbing = false;
  $("seek").classList.remove("dragging");
  syncTime();
});
$("seek").addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") { skip(-15); e.preventDefault(); }
  if (e.key === "ArrowRight") { skip(30); e.preventDefault(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("sleep-menu").hidden) closeSleepMenu();
  else closeSheet();
});

onMedia("play", () => {
  syncPlayButtons();
  if (media === videoEl && $("video-overlay").classList.contains("show")) scheduleOverlayHide();
});
onMedia("ratechange", syncSpeed);
// Re-register the lock-screen handlers whenever playback (re)starts; iOS can
// drop them after the audio session was interrupted.
onMedia("playing", () => { if (current) setupMediaSession(current); });
onMedia("pause", () => {
  syncPlayButtons();
  if (media === videoEl) clearTimeout(overlayTimer); // keep a visible overlay up while paused
  if (current && !isDone(current)) {
    setPos(current, media.currentTime);
    render();
  }
});
onMedia("ended", () => {
  wantPlaying = false;
  markCurrentDone();
  if (sleepValue === "end") setSleep(0);
});
onMedia("loadedmetadata", () => {
  $("seek").setAttribute("aria-valuemax", String(Math.floor(media.duration)));
  syncTime();
});

let lastSave = 0;
onMedia("timeupdate", () => {
  syncTime();
  checkSleep();
  if (!current || isDone(current)) return;
  if (media.duration && media.currentTime >= media.duration * DONE_AT) {
    markCurrentDone();
  } else if (Date.now() - lastSave > 5000) {
    setPos(current, media.currentTime);
    lastSave = Date.now();
  }
});

// ---- add form -------------------------------------------------------------

$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("add-url").value.trim();
  const mode = new FormData(e.target).get("mode");
  try {
    const res = await fetch("/api/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, mode }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { job_id } = await res.json();
    $("add-url").value = "";
    pollJob(job_id);
  } catch (err) {
    setStatus(`Couldn't add: ${err.message}`);
  }
});

async function pollJob(jobId) {
  const jobs = await (await fetch("/api/jobs")).json();
  const job = jobs[jobId];
  if (!job) return;
  if (job.status === "done") {
    setStatus(`Added “${job.title}”`);
    loadLibrary();
  } else if (job.status === "error") {
    setStatus(`Couldn't add: ${job.error}`, 8000);
  } else {
    setStatus(`Fetching ${job.mode}…`, 0);
    setTimeout(() => pollJob(jobId), 2000);
  }
}

// ---- boot -----------------------------------------------------------------

// ---- fast taps ------------------------------------------------------------
// iOS Safari swallows or delays taps for a moment after a list stops scrolling;
// native apps don't. Recognise taps ourselves and activate on touchend. A touch
// that lands while the list is still visibly moving only stops it, as in native
// apps. Speed matters, not timing: iOS keeps nudging a list by fractions of a
// pixel (and firing scroll events) well after it looks still, and taps then
// must still work.
const TAPPABLE = 'button, a[href], [role="menuitemradio"], #sleep-backdrop, video, #video-overlay';
const MOVING_PX_PER_MS = 0.25; // ~250 px/s: faster than this reads as "still scrolling"
let lastScroll = { at: 0, top: 0, speed: 0 };
let tapStart = null;

$("scroller").addEventListener("scroll", (e) => {
  const now = performance.now();
  const top = e.currentTarget.scrollTop;
  const dt = Math.max(1, now - lastScroll.at);
  // Ignore the gap since the previous scroll gesture: speed is only meaningful
  // between events that are close together.
  const speed = dt < 100 ? Math.abs(top - lastScroll.top) / dt : 0;
  lastScroll = { at: now, top, speed };
}, { passive: true });

function listStillMoving() {
  return performance.now() - lastScroll.at < 100 && lastScroll.speed > MOVING_PX_PER_MS;
}

document.addEventListener("touchstart", (e) => {
  if (e.touches.length !== 1) { tapStart = null; return; }
  const t = e.touches[0];
  tapStart = {
    x: t.clientX,
    y: t.clientY,
    at: performance.now(),
    target: e.target,
    stoppingScroll: listStillMoving(),
  };
}, { passive: true }); // a touchstart listener also makes iOS show :active styles

document.addEventListener("touchmove", (e) => {
  if (!tapStart) return;
  const t = e.touches[0];
  if (Math.hypot(t.clientX - tapStart.x, t.clientY - tapStart.y) > 10) tapStart = null;
}, { passive: true });

document.addEventListener("touchend", (e) => {
  const tap = tapStart;
  tapStart = null;
  if (!tap || tap.stoppingScroll || performance.now() - tap.at > 500) return;
  // Leave form controls (sliders, text fields, the Audio/Video toggle) to iOS.
  if (tap.target.closest("input, select, textarea, label")) return;
  const el = tap.target.closest(TAPPABLE);
  if (!el || el.disabled) return;
  e.preventDefault(); // skip Safari's own (delayed) click so it doesn't fire twice
  if (el.matches("video, #video-overlay")) {
    const t = e.changedTouches[0];
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, clientX: t.clientX, clientY: t.clientY }));
  } else {
    el.click();
  }
}, { passive: false });

// iOS can ignore user-scalable=no; cancel its pinch gestures directly.
for (const type of ["gesturestart", "gesturechange", "gestureend"]) {
  document.addEventListener(type, (e) => e.preventDefault(), { passive: false });
}

seenSince(); // start the "new episodes" clock on this device's first launch
document.querySelectorAll("[data-icon]").forEach((el) => el.prepend(icon(el.dataset.icon)));
syncPlayButtons();
syncSpeed();
syncSleep();

// Show the compact nav title once the large title has scrolled under the nav bar.
function syncNav() {
  const navBottom = $("nav").getBoundingClientRect().bottom;
  $("nav").classList.toggle("scrolled", $("view-title").getBoundingClientRect().bottom <= navBottom + 4);
}
$("scroller").addEventListener("scroll", syncNav, { passive: true });

// Each page remembers how far it was scrolled, so going back returns you there.
const scrollMemory = new Map();

function onRoute({ restoreScroll = false } = {}) {
  visitSnapshot.clear();
  render();
  $("scroller").scrollTop = restoreScroll ? (scrollMemory.get(location.hash) || 0) : 0;
  syncNav();
  if (onSettings()) loadSubscriptions();
  else clearTimeout(subsTimer);
}

// How deep a page is: Shows is the top; a show or Settings sits one level in.
function routeDepth(hash) {
  return !hash || hash === "#/" ? 0 : 1;
}

// Move to another page with an iOS-style push/pop slide (or a quick fade between
// pages at the same depth, like the Settings tabs). Uses view transitions where
// the browser supports them; elsewhere it simply switches.
function navigate(href) {
  if (href === location.hash) return;
  const from = routeDepth(location.hash);
  const to = routeDepth(href);
  const dir = to > from ? "forward" : to < from ? "back" : "fade";
  scrollMemory.set(location.hash, $("scroller").scrollTop);
  const go = () => {
    history.replaceState(null, "", href);
    onRoute({ restoreScroll: dir === "back" });
  };
  if (!document.startViewTransition) return go();
  // A tap mid-slide starts a new transition, which ends the old one; only the
  // latest navigation may clear the direction, or it would wipe the new one's.
  const token = ++navToken;
  document.documentElement.dataset.nav = dir;
  const transition = document.startViewTransition(go);
  transition.finished.finally(() => {
    if (token === navToken) delete document.documentElement.dataset.nav;
  });
}
let navToken = 0;

// In-app links replace the current history entry instead of adding one, so
// iOS's edge swipe has nothing to go "back" to (it would drag the app's pages,
// or the player, sideways). The on-screen back button still works.
document.addEventListener("click", (e) => {
  const a = e.target.closest('a[href^="#/"]');
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  navigate(a.getAttribute("href"));
});
window.addEventListener("hashchange", () => onRoute());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    if (current?.mode === "video") {
      const early = videoEl.paused ? ` (iOS paused it ${Math.round(performance.now() - videoPausedAt)}ms earlier)` : "";
      clientLog(`hidden: video ${videoEl.paused ? "already paused" : "playing"}${early}, wantPlaying=${wantPlaying}`);
    }
    handOffToAudio();
    return;
  }
  handBackToVideo();
  loadLibrary(true);
  if (onSettings()) loadSubscriptions();
});
if (onSettings()) loadSubscriptions();
window.addEventListener("online", render);
window.addEventListener("offline", render);
if (canOffline) navigator.serviceWorker.register("sw.js");
loadLibrary();
