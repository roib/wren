const $ = (id) => document.getElementById(id);
const media = $("media");
const SPEEDS = [1, 1.25, 1.5, 1.75, 2];
const MEDIA_CACHE = "oddo-media-v1"; // must match sw.js
const DONE_AT = 0.97; // fraction of duration that counts as "played"
const RING = 2 * Math.PI * 8; // circumference of the download progress ring

// Cache API + service workers only exist on HTTPS (or localhost).
const canOffline = "caches" in window && "serviceWorker" in navigator;

let episodes = [];
let downloaded = new Set(); // media_url paths stored in MEDIA_CACHE
const downloading = new Map(); // episode key -> progress 0..1
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

// ---- data -----------------------------------------------------------------

async function loadLibrary() {
  try {
    const res = await fetch("/api/library");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    episodes = await res.json();
    lsSet("library", JSON.stringify(episodes)); // fallback for offline launches
  } catch {
    try { episodes = JSON.parse(lsGet("library")) || []; } catch { episodes = []; }
    setStatus("Offline — downloaded episodes still play", 5000);
  }
  await loadDownloaded();
  render();
}

async function loadDownloaded() {
  if (!canOffline) return;
  const cache = await caches.open(MEDIA_CACHE);
  const keys = await cache.keys();
  downloaded = new Set(
    keys.map((r) => new URL(r.url).pathname).filter((p) => !p.endsWith(".jpg"))
  );
}

function groupShows(eps) {
  const shows = new Map();
  for (const ep of eps) {
    const name = ep.channel || "Unknown";
    if (!shows.has(name)) shows.set(name, []);
    shows.get(name).push(ep);
  }
  const newest = (ep) => `${ep.upload_date || ""}${String(ep.added_at).padStart(12, "0")}`;
  return [...shows].map(([name, list]) => {
    list.sort((a, b) => newest(b).localeCompare(newest(a)));
    return { name, episodes: list };
  }).sort((a, b) => newest(b.episodes[0]).localeCompare(newest(a.episodes[0])));
}

// ---- rendering --------------------------------------------------------------

function currentShow() {
  const m = location.hash.match(/^#\/show\/(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

function render() {
  const inSettings = location.hash === "#/settings";
  const showName = currentShow();
  const shows = groupShows(episodes);
  const show = showName && shows.find((s) => s.name === showName);

  $("back").hidden = !(show || inSettings);
  $("settings-link").hidden = !!(show || inSettings);
  $("view-title").textContent = inSettings ? "Settings" : show ? show.name : "Shows";
  $("list").hidden = inSettings;
  $("settings").hidden = !inSettings;

  let sub = "";
  if (show) {
    const unplayed = show.episodes.filter((ep) => !isDone(ep)).length;
    sub = [plural(show.episodes.length, "episode"), unplayed ? `${unplayed} unplayed` : null]
      .filter(Boolean).join(" · ");
  }
  $("view-sub").textContent = sub;
  $("view-sub").hidden = !sub;

  if (inSettings) {
    renderSettings();
    return;
  }

  const items = show ? show.episodes.map(episodeRow) : shows.map(showRow);
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
  const art = show.episodes.find((ep) => ep.thumb_url)?.thumb_url;

  return h("li", {},
    h("a", { class: "show", href: `#/show/${encodeURIComponent(show.name)}` },
      h("img", { src: art ?? "", alt: "", loading: "lazy" }),
      h("span", { class: "show-text" },
        h("span", { class: "show-name" }, show.name),
        h("span", { class: "show-sub" },
          unplayed ? h("span", { class: "dot" }) : null,
          unplayed ? `${unplayed} unplayed` : plural(show.episodes.length, "episode"))),
      h("span", { class: "chev" }, icon("chevronRight"))));
}

function episodeRow(ep) {
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
    return h("button", {
      class: "icon-btn sm dl on", "aria-label": "Remove download",
      onclick: () => removeDownload(ep),
    }, icon("downloaded"));
  }
  return h("button", {
    class: "icon-btn sm dl", "aria-label": "Download for offline",
    onclick: () => download(ep),
  }, icon("download"));
}

function renderSettings() {
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
  current = ep;

  const isVideo = ep.mode === "video";
  $("mini").hidden = false;
  document.body.classList.add("has-player");
  $("sheet").classList.toggle("is-video", isVideo);
  $("mini-title").textContent = ep.title;
  $("mini-sub").textContent = ep.channel ?? "";
  $("mini-thumb").src = ep.thumb_url ?? "";
  $("sheet-title").textContent = ep.title;
  $("sheet-channel").textContent = ep.channel ?? "";
  $("sheet-art").src = ep.thumb_url ?? "";
  media.poster = isVideo ? (ep.thumb_url ?? "") : "";
  media.src = ep.media_url;
  media.playbackRate = Number(lsGet("speed")) || 1;

  const resumeAt = getPos(ep);
  media.addEventListener("loadedmetadata", () => {
    if (resumeAt && resumeAt < media.duration - 5) media.currentTime = resumeAt;
  }, { once: true });

  media.play().catch(() => {}); // autoplay may be blocked until user gesture
  setupMediaSession(ep);
  if (isVideo) openSheet();
  render();
}

function openSheet() {
  $("sheet").classList.add("open");
  $("sheet").setAttribute("aria-hidden", "false");
}

function closeSheet() {
  $("sheet").classList.remove("open");
  $("sheet").setAttribute("aria-hidden", "true");
}

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
  ms.setActionHandler("play", () => media.play());
  ms.setActionHandler("pause", () => media.pause());
  ms.setActionHandler("seekbackward", (d) => skip(-(d.seekOffset || 15)));
  ms.setActionHandler("seekforward", (d) => skip(d.seekOffset || 30));
  ms.setActionHandler("seekto", (d) => { media.currentTime = d.seekTime; });
}

function skip(delta) {
  media.currentTime = Math.max(0, Math.min(media.duration || 0, media.currentTime + delta));
}

function togglePlay() {
  media.paused ? media.play().catch(() => {}) : media.pause();
}

function syncPlayButtons() {
  const name = media.paused ? "play" : "pause";
  const label = media.paused ? "Play" : "Pause";
  for (const id of ["play", "mini-play"]) {
    $(id).innerHTML = svg(name);
    $(id).setAttribute("aria-label", label);
  }
}

function syncSpeed() {
  $("speed").textContent = `${media.playbackRate}×`;
}

function syncTime() {
  const d = media.duration || 0;
  const t = media.currentTime || 0;
  const pct = d ? (t / d) * 100 : 0;
  $("seek").value = Math.floor(t);
  $("seek").style.setProperty("--pct", `${pct}%`);
  $("mini-bar").style.width = `${pct}%`;
  $("cur").textContent = fmt(t);
  $("remain").textContent = `-${fmt(d - t)}`;
}

$("play").addEventListener("click", togglePlay);
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
$("seek").addEventListener("input", (e) => { media.currentTime = Number(e.target.value); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeSheet();
});

media.addEventListener("play", syncPlayButtons);
media.addEventListener("ratechange", syncSpeed);
media.addEventListener("pause", () => {
  syncPlayButtons();
  if (current && !isDone(current)) {
    setPos(current, media.currentTime);
    render();
  }
});
media.addEventListener("ended", markCurrentDone);
media.addEventListener("loadedmetadata", () => {
  $("seek").max = Math.floor(media.duration);
  syncTime();
});

let lastSave = 0;
media.addEventListener("timeupdate", () => {
  syncTime();
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

document.querySelectorAll("[data-icon]").forEach((el) => el.prepend(icon(el.dataset.icon)));
syncPlayButtons();
syncSpeed();

window.addEventListener("hashchange", () => { render(); window.scrollTo(0, 0); });
window.addEventListener("online", render);
window.addEventListener("offline", render);
if (canOffline) navigator.serviceWorker.register("sw.js");
loadLibrary();
