// Wren: manage the shows the server downloads. Playback happens in Apple
// Podcasts (or any podcast app) through each show's feed; this app subscribes,
// shows what's downloaded and what's happening, and can test-play an episode.

const $ = (id) => document.getElementById(id);

// ---- icons ----------------------------------------------------------------

const ICONS = {
  play: '<path d="M8 5.6v12.8a1 1 0 0 0 1.5.86l10.2-6.4a1 1 0 0 0 0-1.72L9.5 4.74A1 1 0 0 0 8 5.6z" fill="currentColor" stroke="none"/>',
  download: '<path d="M12 4.5v10.5M7.5 10.5 12 15l4.5-4.5M5.5 19.5h13"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  pulse: '<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>',
  podcast: '<circle cx="12" cy="10.5" r="2.2"/><path d="M12 12.7V20"/><path d="M8.3 14.6a5.2 5.2 0 1 1 7.4 0"/><path d="M5.7 17.3a8.8 8.8 0 1 1 12.6 0"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2.2"/><path d="M15.5 8.5V6.7a2.2 2.2 0 0 0-2.2-2.2H6.7a2.2 2.2 0 0 0-2.2 2.2v6.6a2.2 2.2 0 0 0 2.2 2.2h1.8"/>',
  external: '<path d="M14 5h5v5M19 5l-8 8"/><path d="M18 14v4a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18V7.5A1.5 1.5 0 0 1 5.5 6H10"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
  trash: '<path d="M4.5 7h15M9.5 7V5.2c0-.66.54-1.2 1.2-1.2h2.6c.66 0 1.2.54 1.2 1.2V7M6.5 7l.8 11.6a1.6 1.6 0 0 0 1.6 1.4h6.2a1.6 1.6 0 0 0 1.6-1.4L17.5 7"/>',
  alert: '<path d="M12 4.2 3 19.5h18z"/><path d="M12 10v4.2M12 17.2v.1"/>',
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

// ---- helpers --------------------------------------------------------------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (v !== null && v !== undefined && v !== false) el.setAttribute(k, v === true ? "" : v);
  }
  el.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

function fmtDuration(sec) {
  if (!sec) return null;
  const m = Math.max(1, Math.round(sec / 60));
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

function fmtDate(yyyymmdd) {
  if (!yyyymmdd) return null;
  const d = new Date(`${yyyymmdd.slice(0, 4)}-${yyyymmdd.slice(4, 6)}-${yyyymmdd.slice(6, 8)}T00:00`);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

function fmtBytes(n) {
  if (!n) return null;
  if (n >= 1e12) return `${(n / 1e12).toFixed(1)} TB`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  return `${Math.max(1, Math.round(n / 1e6))} MB`;
}

function fmtClock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

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

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const join = (...parts) => parts.filter(Boolean).join(" · ");

let toastTimer;
function toast(text, ms = 3500) {
  const t = $("toast");
  t.textContent = text;
  t.classList.toggle("show", !!text);
  clearTimeout(toastTimer);
  if (text && ms) toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

// fetch JSON; throws an Error carrying the server's message.
async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function friendlyError(err) {
  if (!err) return "";
  if (/not a bot|Sign in to confirm/i.test(err)) return "YouTube wants to confirm the server isn't a bot";
  if (/429|Too Many Requests/.test(err)) return "YouTube is rate-limiting the server";
  if (/unavailable|private|removed/i.test(err)) return "Video unavailable";
  if (/^too short/.test(err)) return err.replace("too short", "Too short");
  if (err === "older than backfill") return "Older than the episodes picked at subscribe";
  if (err === "title filter") return "Didn't match the title filter";
  if (err === "deleted") return "Deleted from the server";
  return err.replace(/^(feed: )?ERROR:\s*(\[\w+\]\s*[\w-]+:\s*)?/, "").slice(0, 140);
}

const feedUrl = (path) => new URL(path, location.href).href;
// Apple Podcasts opens podcast:// links with "Follow a Show by URL" filled in.
const podcastLink = (path) => `podcast://${location.host}${path}`;

async function copyText(text, done = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
    toast(done);
  } catch {
    toast(text, 10000); // clipboard unavailable: show it to copy by hand
  }
}

// ---- format badges ---------------------------------------------------------

function badge(mode) {
  return h("span", { class: `badge ${mode}` }, mode === "video" ? "Video" : "Audio");
}

// AUDIO, VIDEO, or AUDIO + VIDEO when an audio show also gets video copies.
function showBadges(s) {
  return h("span", { class: "badges" },
    s.mode === "video" ? badge("video") : badge("audio"),
    s.mode === "audio" && s.video_copy ? badge("video") : null,
    s.enabled ? null : h("span", { class: "badge muted" }, "Paused"));
}

// ---- state & routing --------------------------------------------------------

let overview = null; // /api/overview
const details = new Map(); // show id ("singles" for single episodes) -> detail
let loadError = null;

function route() {
  const [path, query = ""] = location.hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(query);
  let m;
  if ((m = path.match(/^\/show\/(\d+)$/))) return { name: "show", id: Number(m[1]), params };
  if (path === "/singles") return { name: "singles", id: "singles", params };
  if (path === "/add") return { name: "add", params };
  if (path === "/status") return { name: "status", params };
  return { name: "home", params };
}

// How deep a page is: Shows is the top; everything else sits one level in.
function routeDepth(hash) {
  return !hash || hash === "#/" || hash === "#" ? 0 : 1;
}

async function load() {
  const r = route();
  try {
    if (r.name === "show") details.set(r.id, await api(`/api/shows/${r.id}/detail`));
    else if (r.name === "singles") details.set("singles", await api("/api/singles"));
    else if (r.name !== "add") overview = await api("/api/overview");
    loadError = null;
  } catch (err) {
    loadError = err.message;
  }
  if (route().name === r.name) render();
  schedule();
}

// Poll quickly while the server is busy so progress shows up; slowly otherwise.
let pollTimer = null;
function schedule() {
  clearTimeout(pollTimer);
  const r = route();
  if (r.name === "add") return;
  const data = r.name === "show" || r.name === "singles" ? details.get(r.id) : overview;
  pollTimer = setTimeout(() => {
    if (document.visibilityState === "visible") load();
    else schedule();
  }, isBusy(data) ? 4000 : 30000);
}

function isBusy(data) {
  if (!data) return false;
  const w = data.worker;
  if (w && ["downloading", "checking shows"].includes(w.activity)) return true;
  const entries = data.entries || [];
  return entries.some((e) => ["downloading", "queued"].includes(e.state)) || (data.pending || []).length > 0;
}

// ---- rendering --------------------------------------------------------------

function render() {
  const r = route();
  const top = r.name === "home";
  $("back").hidden = top;
  $("nav-actions").hidden = !top;

  // While a settings field is being edited, leave the page alone so a refresh
  // doesn't wipe what's being typed.
  if (r.name === "show" && document.activeElement?.closest?.("#view .settings")) return;

  let title = "Shows";
  let sub = "";
  let body;
  if (r.name === "show") {
    const d = details.get(r.id);
    title = d?.show.name ?? "";
    body = d ? showView(d) : placeholder();
  } else if (r.name === "singles") {
    title = "Single episodes";
    const d = details.get("singles");
    body = d ? singlesView(d) : placeholder();
  } else if (r.name === "add") {
    title = "Add";
    body = addView(r.params);
  } else if (r.name === "status") {
    title = "Status";
    body = overview ? statusView(overview) : placeholder();
  } else {
    sub = overview ? join(plural(overview.shows.length, "show"), plural(overview.totals.episodes, "episode"),
      fmtBytes(overview.totals.bytes)) : "";
    body = overview ? homeView(overview) : placeholder();
  }
  $("view-title").textContent = title;
  $("nav-title").textContent = title;
  $("view-sub").textContent = sub;
  $("view-sub").hidden = !sub;
  const error = loadError && r.name !== "add" ? banner(`Can't reach the server: ${loadError}`) : null;
  $("view").replaceChildren(...[error, body].filter(Boolean));
}

function placeholder() {
  return h("p", { class: "empty" }, loadError ? "" : "Loading…");
}

function banner(text) {
  return h("div", { class: "banner" }, icon("alert"), h("span", {}, text));
}

function section(title, ...children) {
  return h("section", { class: "section" }, title ? h("h2", {}, title) : null, ...children);
}

function art(url, cls = "art") {
  return url ? h("img", { class: cls, src: url, alt: "", loading: "lazy" }) : h("span", { class: `${cls} blank` });
}

// ---- home -------------------------------------------------------------------

const FILTER_AFTER = 6; // show the filter box once there are more shows than this
let filter = "";

function workerLine(data) {
  const w = data.worker;
  const pending = data.pending || [];
  const downloading = pending.find((p) => p.state === "downloading");
  const queued = pending.filter((p) => p.state === "queued").length;
  let text;
  let busy = false;
  if (w.paused_until) text = `Paused · YouTube is rate-limiting until ${fmtClock(w.paused_until)}`;
  else if (w.activity === "checking shows") { text = "Checking for new episodes…"; busy = true; }
  else if (downloading) { text = `Downloading “${downloading.title}”`; busy = true; }
  else if (w.activity === "downloading") { text = "Downloading…"; busy = true; }
  else if (queued && !w.active_now && !w.override) text = `${queued} queued · downloads resume ${fmtClock(w.resume_at)}`;
  else if (queued && w.activity === "hourly limit") text = `${queued} queued · hourly limit reached`;
  else if (queued) text = `${queued} queued`;
  else text = "Up to date";
  return { text, busy, next: `Next check ${until(w.next_poll)}` };
}

function statusCard(data) {
  const line = workerLine(data);
  return h("a", { class: "status-card card", href: "#/status" },
    h("span", { class: `status-dot${line.busy ? " busy" : ""}${data.worker.paused_until ? " paused" : ""}` }),
    h("span", { class: "row-text" },
      h("span", { class: "status-main" }, line.text),
      h("span", { class: "row-meta" }, join(line.next, `${fmtBytes(data.disk.free)} free`))),
    h("span", { class: "chev" }, icon("chevronRight")));
}

function homeView(data) {
  if (!data.shows.length && !data.singles.episodes) {
    return h("div", { class: "empty" },
      h("p", {}, "No shows yet."),
      h("a", { class: "button strong", href: "#/add" }, icon("plus"), "Add a show"));
  }
  const matching = () => {
    const q = filter.trim().toLowerCase();
    return q ? data.shows.filter((s) => s.name.toLowerCase().includes(q)) : data.shows;
  };
  const filterBox = data.shows.length > FILTER_AFTER ? h("input", {
    class: "filter", type: "search", placeholder: "Filter shows", value: filter, autocomplete: "off",
    oninput: (e) => {
      filter = e.target.value;
      document.querySelector(".show-list").replaceChildren(...showRows(matching()));
    },
  }) : null;
  return h("div", {},
    statusCard(data),
    filterBox,
    data.shows.length ? section("Shows", h("ul", { class: "show-list card list-card" }, ...showRows(matching()))) : null,
    data.singles.episodes ? section("Other",
      h("ul", { class: "card list-card" }, singlesRow(data.singles))) : null);
}

function showRows(shows) {
  if (!shows.length) return [h("li", { class: "none" }, "No shows match.")];
  return shows.map(showRow);
}

function showStatus(s) {
  if (s.downloading) return h("span", { class: "row-status" }, h("span", { class: "spinner" }), "Downloading…");
  if (s.last_error) return h("span", { class: "row-status warn" }, icon("alert"), friendlyError(s.last_error));
  if (s.failed) return h("span", { class: "row-status warn" }, icon("alert"), `${plural(s.failed, "download")} failed`);
  if (s.retrying) return h("span", { class: "row-status warn" }, `Retrying ${plural(s.retrying, "download")}`);
  if (s.queued) return h("span", { class: "row-status" }, `${s.queued} queued`);
  if (s.mode === "audio" && s.video_copy && s.video_copies < s.episodes) {
    return h("span", { class: "row-status" }, `Video copies ${s.video_copies} of ${s.episodes}`);
  }
  return null;
}

function showRow(s) {
  return h("li", {},
    h("a", { class: "row", href: `#/show/${s.id}` },
      art(s.art_url),
      h("span", { class: "row-text" },
        h("span", { class: "row-title" }, s.name),
        h("span", { class: "row-meta" }, showBadges(s),
          h("span", {}, join(plural(s.episodes, "episode"), fmtDate(s.newest_date), fmtBytes(s.bytes)))),
        showStatus(s)),
      h("span", { class: "chev" }, icon("chevronRight"))));
}

function singlesRow(singles) {
  return h("li", {},
    h("a", { class: "row", href: "#/singles" },
      art(singles.art_url),
      h("span", { class: "row-text" },
        h("span", { class: "row-title" }, "Single episodes"),
        h("span", { class: "row-meta" }, h("span", {}, join(plural(singles.episodes, "episode"), fmtBytes(singles.bytes))))),
      h("span", { class: "chev" }, icon("chevronRight"))));
}

// ---- show page --------------------------------------------------------------

let expanded = null; // video id of the episode row showing its actions
const showSkipped = new Set(); // show ids with their skipped list open

function feedButtons(path, label) {
  return h("div", { class: "feed-buttons" },
    h("a", { class: "button strong", href: podcastLink(path) }, icon("podcast"), label),
    h("button", { class: "button", onclick: () => copyText(feedUrl(path), "Feed URL copied") }, icon("copy"), "Copy feed"));
}

function showHero(s) {
  return h("div", { class: "hero card" },
    h("div", { class: "hero-top" },
      art(s.art_url, "hero-art"),
      h("div", { class: "hero-info" },
        showBadges(s),
        h("div", { class: "hero-meta" }, join(plural(s.episodes, "episode"), fmtBytes(s.bytes))),
        h("div", { class: "hero-meta" }, join(`Checked ${ago(s.last_checked)}`,
          s.keep_last > 0 ? `keeps latest ${s.keep_last}` : "keeps everything")))),
    feedButtons(s.feed_path, "Open in Podcasts"),
    s.mode === "audio" && s.video_copy ? feedButtons(s.video_feed_path, "Open video feed") : null,
    h("div", { class: "hero-actions" },
      h("button", { class: "action", onclick: () => showCommand(s, "refresh", `Checking ${s.name} for new episodes…`) },
        icon("refresh"), "Check now"),
      h("button", { class: "action", onclick: () => showCommand(s, "download-now", `Downloading ${s.name} now…`) },
        icon("download"), "Download now"),
      h("a", { class: "action subtle", href: s.source_url, target: "_blank", rel: "noopener" }, icon("external"), "YouTube")));
}

async function showCommand(s, cmd, msg) {
  try {
    await api(`/api/shows/${s.id}/${cmd}`, { method: "POST" });
    toast(msg);
  } catch (err) {
    toast(`Couldn't start: ${err.message}`);
  }
  setTimeout(load, 1500);
}

const IN_PROGRESS = ["downloading", "queued", "retrying", "premiere"];

function entrySections(entries, { showId, onRetry }) {
  const progress = entries.filter((e) => IN_PROGRESS.includes(e.state));
  const done = entries.filter((e) => e.state === "downloaded");
  const failed = entries.filter((e) => e.state === "failed");
  const skipped = entries.filter((e) => e.state === "skipped");
  const open = showSkipped.has(showId);
  return [
    progress.length ? section("In progress", h("ul", { class: "card list-card" }, progress.map((e) => pendingRow(e)))) : null,
    failed.length ? section("Failed", h("ul", { class: "card list-card" }, failed.map((e) => pendingRow(e, onRetry)))) : null,
    section("Downloaded", done.length
      ? h("ul", { class: "card list-card" }, done.map(episodeRow))
      : h("p", { class: "none card" }, progress.length ? "Nothing downloaded yet." : "No episodes downloaded.")),
    skipped.length ? section(null,
      h("button", {
        class: "disclosure",
        "aria-expanded": String(open),
        onclick: () => { open ? showSkipped.delete(showId) : showSkipped.add(showId); render(); },
      }, `${open ? "Hide" : "Show"} ${plural(skipped.length, "skipped episode")}`, h("span", { class: "chev" }, icon("chevronDown"))),
      open ? h("ul", { class: "card list-card" }, skipped.map((e) => pendingRow(e, onRetry))) : null) : null,
  ];
}

function stateText(e) {
  switch (e.state) {
    case "downloading": return [h("span", { class: "spinner" }), "Downloading…"];
    case "queued": return ["Queued"];
    case "premiere": return ["Premieres soon"];
    case "retrying": return [`Retrying · ${friendlyError(e.error)}`];
    case "failed": return [friendlyError(e.error) || "Failed"];
    case "skipped": return [friendlyError(e.error) || "Skipped"];
    default: return [];
  }
}

function pendingRow(e, onRetry) {
  const canRetry = onRetry && (e.state === "failed" || e.state === "skipped");
  return h("li", { class: `ep pending ${e.state}` },
    h("div", { class: "ep-head static" },
      art(e.thumb_url, "ep-thumb"),
      h("span", { class: "ep-text" },
        h("span", { class: "ep-title" }, e.title || e.source_url),
        h("span", { class: "ep-meta" }, ...stateText(e), e.duration ? ` · ${fmtDuration(e.duration)}` : null)),
      canRetry ? h("button", { class: "action", onclick: () => onRetry(e) }, e.state === "failed" ? "Retry" : "Download") : null));
}

function episodeRow(e) {
  const open = expanded === e.video_id;
  const size = e.copies.reduce((n, c) => n + c.bytes, 0);
  return h("li", { class: `ep${open ? " open" : ""}` },
    h("button", {
      class: "ep-head",
      "aria-expanded": String(open),
      // Toggle classes in place (no re-render) so the row animates open/closed.
      onclick: (ev) => {
        const li = ev.currentTarget.closest(".ep");
        const was = li.classList.contains("open");
        document.querySelectorAll(".ep.open").forEach((x) => x.classList.remove("open"));
        if (!was) li.classList.add("open");
        expanded = was ? null : e.video_id;
      },
    },
      art(e.thumb_url, "ep-thumb"),
      h("span", { class: "ep-text" },
        h("span", { class: "ep-title" }, e.title),
        h("span", { class: "ep-meta" },
          h("span", { class: "badges" }, e.copies.map((c) => badge(c.mode)),
            e.copy_pending ? h("span", { class: "badge muted" }, "Video soon") : null),
          h("span", {}, join(fmtDate(e.upload_date), fmtDuration(e.duration), fmtBytes(size)))))),
    // Always rendered; collapsed with CSS until the row is open.
    h("div", { class: "collapse" },
      h("div", { class: "collapse-body" },
        h("div", { class: "ep-actions" },
          e.copies.map((c) => h("button", { class: "action", onclick: () => openPlayer(e, c) },
            icon("play"), c.mode === "video" ? "Play video" : "Play audio")),
          h("a", { class: "action subtle", href: e.source_url, target: "_blank", rel: "noopener" },
            icon("external"), "YouTube"),
          h("button", { class: "action subtle danger", onclick: (ev) => deleteEpisode(e, ev.currentTarget) },
            icon("trash"), "Delete")))));
}

// Two taps: the first turns the button into "Tap again to delete" for a few seconds.
async function deleteEpisode(e, btn) {
  if (!btn.classList.contains("confirm")) {
    btn.classList.add("confirm");
    btn.lastChild.textContent = e.copies.length > 1 ? "Tap again to delete both" : "Tap again to delete";
    setTimeout(() => { btn.classList.remove("confirm"); btn.lastChild.textContent = "Delete"; }, 3000);
    return;
  }
  btn.disabled = true;
  try {
    await api(`/api/episodes/${encodeURIComponent(e.video_id)}`, { method: "DELETE" });
    toast(`Deleted “${e.title}”`);
    expanded = null;
  } catch (err) {
    btn.disabled = false;
    toast(`Couldn't delete: ${err.message}`);
  }
  load();
}

function showView(d) {
  const s = d.show;
  return h("div", {},
    s.last_error ? banner(friendlyError(s.last_error)) : null,
    showHero(s),
    ...entrySections(d.entries, { showId: s.id, onRetry: (e) => requeue(s, e) }),
    settingsCard(s));
}

async function requeue(s, e) {
  try {
    await api(`/api/shows/${s.id}/items/${encodeURIComponent(e.video_id)}/queue`, { method: "POST" });
    toast(`Downloading “${e.title}”`);
  } catch (err) {
    toast(`Couldn't queue it: ${err.message}`);
  }
  load();
}

// ---- show settings ------------------------------------------------------------

const KEEP_OPTIONS = [[3, "Latest 3"], [5, "Latest 5"], [10, "Latest 10"], [20, "Latest 20"], [50, "Latest 50"], [0, "Everything"]];
const MIN_OPTIONS = [[0, "Off"], [60, "1 min"], [180, "3 min"], [300, "5 min"], [600, "10 min"], [1200, "20 min"]];
let confirmingRemove = null;

async function patchShow(s, fields, msg) {
  document.activeElement?.blur?.(); // let the refresh below re-render the settings
  try {
    await api(`/api/shows/${s.id}`, { method: "PATCH", body: fields });
    if (msg) toast(msg);
  } catch (err) {
    toast(`Couldn't save: ${err.message}`, 6000);
  }
  load();
}

function settingRow(label, control, hint) {
  return h("div", { class: "setting" },
    h("div", { class: "setting-main" }, h("span", { class: "setting-label" }, label), control),
    hint ? h("p", { class: "setting-hint" }, hint) : null);
}

function toggle(on, onchange, label) {
  return h("button", {
    class: "switch", role: "switch", "aria-checked": String(on), "aria-label": label,
    onclick: (e) => { e.currentTarget.setAttribute("aria-checked", String(!on)); onchange(!on); },
  });
}

function select(options, value, onchange) {
  const all = options.some(([v]) => v === value) ? options : [[value, String(value)], ...options];
  return h("select", { class: "select", onchange: (e) => onchange(Number(e.target.value)) },
    all.map(([v, text]) => h("option", { value: String(v), selected: v === value }, text)));
}

function settingsCard(s) {
  const confirming = confirmingRemove === s.id;
  return section("Settings", h("div", { class: "settings card" },
    settingRow("Format", h("div", { class: "segmented small", role: "radiogroup", "aria-label": "Format" },
      ["audio", "video"].map((m) => h("label", {},
        h("input", {
          type: "radio", name: `mode-${s.id}`, value: m, checked: s.mode === m,
          onchange: () => patchShow(s, { mode: m }, `New episodes will download as ${m}`),
        }),
        h("span", {}, m === "audio" ? "Audio" : "Video")))),
      "Applies to episodes downloaded from now on."),
    s.mode === "audio" ? settingRow("Also download video",
      toggle(!!s.video_copy, (on) => patchShow(s, { video_copy: on },
        on ? `Downloading ${s.name} as video too` : "Video copies off"), "Also download video"),
      "Adds a separate video feed alongside the audio one.") : null,
    settingRow("Check for new episodes", toggle(!!s.enabled, (on) => patchShow(s, { enabled: on },
      on ? `Checking ${s.name} again` : `Paused ${s.name}`), "Check for new episodes")),
    settingRow("Keep", select(KEEP_OPTIONS, s.keep_last, (v) => patchShow(s, { keep_last: v }, "Saved"))),
    settingRow("Skip shorter than", select(MIN_OPTIONS, s.min_duration, (v) => patchShow(s, { min_duration: v }, "Saved")),
      "Leaves out Shorts and clips."),
    settingRow("Title filter", h("input", {
      class: "text-input", type: "text", value: s.title_filter || "", placeholder: "Any title",
      autocomplete: "off", autocapitalize: "off", spellcheck: "false",
      onkeydown: (e) => { if (e.key === "Enter") e.target.blur(); },
      onchange: (e) => patchShow(s, { title_filter: e.target.value.trim() || null }, "Saved"),
    }), "Only download episodes whose title matches (a regular expression)."),
    h("button", { class: `remove${confirming ? " confirm" : ""}`, onclick: () => removeShow(s) },
      confirming ? "Tap again to remove" : "Remove show")));
}

async function removeShow(s) {
  if (confirmingRemove !== s.id) {
    confirmingRemove = s.id;
    render();
    setTimeout(() => { if (confirmingRemove === s.id) { confirmingRemove = null; render(); } }, 3000);
    return;
  }
  confirmingRemove = null;
  try {
    await api(`/api/shows/${s.id}`, { method: "DELETE" });
    toast(`Removed ${s.name}. Its episodes moved to Single episodes.`, 5000);
    navigate("#/");
  } catch (err) {
    toast(`Couldn't remove: ${err.message}`);
  }
}

// ---- single episodes -----------------------------------------------------------

function singlesView(d) {
  return h("div", {},
    h("div", { class: "hero card" },
      h("div", { class: "hero-top" },
        art(overview?.singles.art_url ?? "/media/shows/singles.jpg", "hero-art"),
        h("div", { class: "hero-info" },
          h("div", { class: "hero-meta" }, "Episodes added one at a time, and those left from removed shows."))),
      feedButtons("/feeds/singles.xml", "Open in Podcasts")),
    ...entrySections(d.entries, { showId: "singles", onRetry: retryJob }));
}

async function retryJob(e) {
  try {
    await api("/api/add", { method: "POST", body: { url: e.source_url, mode: e.mode || "audio" } });
    toast("Trying again…");
  } catch (err) {
    toast(`Couldn't retry: ${err.message}`);
  }
  load();
}

// ---- add ------------------------------------------------------------------------

const add = { url: "", shared: null, busy: false, result: null, error: null, epMode: "audio", subs: {} };

function addView(params) {
  // A link from the "Share to Wren" shortcut: look it up straight away.
  const shared = params.get("url");
  if (shared && shared !== add.shared) {
    add.shared = shared;
    add.url = shared;
    setTimeout(lookup, 0);
  }
  const canPaste = !!navigator.clipboard?.readText;
  return h("div", {},
    h("form", { class: "card add-form", onsubmit: (e) => { e.preventDefault(); lookup(); } },
      h("div", { class: "input-row" },
        h("input", {
          id: "add-url", type: "url", value: add.url, placeholder: "YouTube channel, playlist or video",
          autocomplete: "off", autocapitalize: "off", spellcheck: "false",
          oninput: (e) => { add.url = e.target.value; },
        }),
        canPaste ? h("button", { type: "button", class: "action", onclick: pasteLink }, "Paste") : null),
      h("button", { type: "submit", class: "primary", disabled: add.busy }, add.busy ? "Looking up…" : "Look up")),
    add.error ? banner(add.error) : null,
    add.result ? resultView(add.result) : null,
    shareHelp());
}

async function pasteLink() {
  try {
    add.url = (await navigator.clipboard.readText()).trim();
    render();
    lookup();
  } catch {
    toast("Couldn't read the clipboard");
  }
}

async function lookup() {
  const url = add.url.trim();
  if (!url || add.busy) return;
  document.activeElement?.blur?.();
  Object.assign(add, { busy: true, error: null, result: null });
  render();
  try {
    add.result = await api("/api/resolve", { method: "POST", body: { url } });
  } catch (err) {
    add.error = err.message;
  }
  add.busy = false;
  if (route().name === "add") render();
}

// Radio pickers write into `state[key]`; `name` keeps several groups on one
// page apart (the episode, a playlist and a channel can all be offered at once).
function modePicker(state, key, name) {
  return h("div", { class: "segmented", role: "radiogroup", "aria-label": "Format" },
    ["audio", "video"].map((m) => h("label", {},
      h("input", { type: "radio", name, value: m, checked: state[key] === m, onchange: () => { state[key] = m; } }),
      h("span", {}, m === "audio" ? "Audio" : "Video"))));
}

function backfillPicker(state, name) {
  return h("div", { class: "segmented four", role: "radiogroup", "aria-label": "Episodes to download now" },
    [1, 3, 5, 10].map((n) => h("label", {},
      h("input", { type: "radio", name, value: n, checked: state.backfill === n, onchange: () => { state.backfill = n; } }),
      h("span", {}, String(n)))));
}

// Each show offered keeps its own format and backfill choice.
const subChoice = (url) => (add.subs[url] ??= { mode: "audio", backfill: 3 });

function subscribeBlock(url, name, subscribed) {
  if (subscribed) {
    return h("a", { class: "row subscribed", href: `#/show/${subscribed.id}` },
      icon("check"), h("span", { class: "row-text" }, `Already subscribed to ${subscribed.name}`),
      h("span", { class: "chev" }, icon("chevronRight")));
  }
  const choice = subChoice(url);
  return h("div", { class: "choice" },
    h("div", { class: "field-label" }, "Format"), modePicker(choice, "mode", `mode-${url}`),
    h("div", { class: "field-label" }, "Download the latest"), backfillPicker(choice, `backfill-${url}`),
    h("button", { class: "primary", onclick: (e) => subscribe(url, name, e.currentTarget) }, `Subscribe to ${name}`));
}

function resultView(r) {
  if (r.kind === "show") {
    return section("Show", h("div", { class: "card result" },
      h("div", { class: "result-title" }, r.title),
      r.latest.length ? h("ul", { class: "preview" }, r.latest.map((i) => h("li", {},
        h("span", { class: "preview-title" }, i.title),
        h("span", { class: "preview-meta" }, i.live ? "Live" : fmtDuration(i.duration) || "")))) : null,
      subscribeBlock(r.url, r.title, r.subscribed)));
  }
  return h("div", {},
    section("Episode", h("div", { class: "card result" },
      h("div", { class: "result-video" },
        art(r.thumb_url, "result-thumb"),
        h("div", { class: "row-text" },
          h("div", { class: "result-title" }, r.title),
          h("div", { class: "row-meta" }, join(r.channel, fmtDuration(r.duration))))),
      modePicker(add, "epMode", "ep-mode"),
      h("button", { class: "button strong full", onclick: (e) => addEpisode(r.url, e.currentTarget) }, "Add just this episode"))),
    r.playlist ? section("Playlist", h("div", { class: "card result" },
      r.playlist.subscribed ? null : h("div", { class: "result-title" }, r.playlist.title),
      subscribeBlock(r.playlist.url, r.playlist.title, r.playlist.subscribed))) : null,
    r.channel_url ? section("Whole channel", h("div", { class: "card result" },
      r.subscribed ? null : h("div", { class: "result-title" }, r.channel),
      subscribeBlock(r.channel_url, r.channel, r.subscribed))) : null);
}

async function subscribe(url, name, btn) {
  btn.disabled = true;
  btn.textContent = "Subscribing…";
  try {
    const { mode, backfill } = subChoice(url);
    const show = await api("/api/shows", { method: "POST", body: { url, mode, backfill } });
    toast(`Subscribed to ${show.name}. Downloading the latest ${backfill}.`, 5000);
    Object.assign(add, { url: "", result: null, error: null, subs: {} });
    navigate(`#/show/${show.id}`);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = `Subscribe to ${name}`;
    toast(`Couldn't subscribe: ${err.message}`, 6000);
  }
}

async function addEpisode(url, btn) {
  btn.disabled = true;
  try {
    await api("/api/add", { method: "POST", body: { url, mode: add.epMode } });
    toast(`Downloading the ${add.epMode}…`, 4000);
    Object.assign(add, { url: "", result: null, error: null, subs: {} });
    navigate("#/singles");
  } catch (err) {
    btn.disabled = false;
    toast(`Couldn't add: ${err.message}`, 6000);
  }
}

let shareOpen = false;
function shareHelp() {
  const template = `${location.origin}/#/add?url=`;
  return section(null,
    h("button", { class: "disclosure", "aria-expanded": String(shareOpen), onclick: () => { shareOpen = !shareOpen; render(); } },
      "Set up “Share to Wren”", h("span", { class: "chev" }, icon("chevronDown"))),
    shareOpen ? h("div", { class: "card help" },
      h("p", {}, "Put Wren in the share sheet of Safari and the YouTube app with a shortcut:"),
      h("ol", {},
        h("li", {}, "In Shortcuts, tap +, and name the shortcut “Share to Wren”."),
        h("li", {}, "Tap ⓘ, turn on Show in Share Sheet, and have it receive URLs and Safari web pages."),
        h("li", {}, "Add the URL Encode action, encoding the Shortcut Input."),
        h("li", {}, "Add a Text action with the link below, then insert URL Encoded Text at its end."),
        h("li", {}, "Add Open URLs, opening that Text.")),
      h("div", { class: "template" }, h("code", {}, template),
        h("button", { class: "action", onclick: () => copyText(template, "Link copied") }, icon("copy"), "Copy")),
      h("p", { class: "setting-hint" },
        "Sharing a video offers that episode or its whole show. iOS opens shortcut links in Safari, not the home-screen app.")) : null);
}

// ---- status -------------------------------------------------------------------

function kv(label, value) {
  return h("div", { class: "kv" }, h("span", {}, label), h("span", {}, value));
}

function statusView(data) {
  const w = data.worker;
  const [from, to] = w.active_hours;
  const hour = (n) => new Date(2000, 0, 1, n % 24).toLocaleTimeString([], { hour: "numeric" });
  const city = w.timezone.split("/").pop().replace(/_/g, " ");
  const jobs = data.jobs.slice(0, 8);
  return h("div", {},
    section("Downloader", h("div", { class: "card list-card" },
      kv("Now", workerLine(data).text),
      kv("Downloads", `${hour(from)}–${hour(to)}, ${city}${w.active_now ? "" : " · resting"}`),
      kv("Last check", ago(w.last_poll)),
      kv("Next check", until(w.next_poll)),
      w.paused_until ? kv("Paused until", fmtClock(w.paused_until)) : null),
      h("div", { class: "hero-actions" },
        h("button", { class: "action", onclick: () => command("/api/refresh", "Checking every show…") }, icon("refresh"), "Check all now"),
        h("button", { class: "action", onclick: () => command("/api/download-now", "Downloading everything queued now…") },
          icon("download"), "Download all now"))),
    section("Queue", data.pending.length
      ? h("ul", { class: "card list-card" }, data.pending.map((p) => h("li", { class: `ep pending ${p.state}` },
        h("div", { class: "ep-head static" },
          art(p.thumb_url, "ep-thumb"),
          h("span", { class: "ep-text" },
            h("span", { class: "ep-title" }, p.title),
            h("span", { class: "ep-meta" }, ...stateText(p), ` · ${p.show}`))))))
      : h("p", { class: "none card" }, "Nothing queued.")),
    jobs.length ? section("Single episodes", h("div", { class: "card list-card" },
      jobs.map((j) => kv(j.title || j.url,
        j.status === "error" ? "Failed" : j.status === "done" ? ago(j.updated_at) : j.status === "running" ? "Downloading" : "Queued")))) : null,
    section("Storage", h("div", { class: "card list-card" },
      kv("Episodes", join(plural(data.totals.episodes, "episode"), fmtBytes(data.totals.bytes) || "0 MB")),
      kv("Free space", `${fmtBytes(data.disk.free)} of ${fmtBytes(data.disk.total)}`))));
}

async function command(path, msg) {
  try {
    await api(path, { method: "POST" });
    toast(msg);
  } catch (err) {
    toast(`Couldn't start: ${err.message}`);
  }
  setTimeout(load, 1500);
}

// ---- test player ------------------------------------------------------------------

function openPlayer(e, copy) {
  const isVideo = copy.mode === "video";
  const el = h(isVideo ? "video" : "audio", { controls: true, playsinline: true, preload: "metadata", src: copy.media_url });
  if (isVideo && copy.subs_url) el.append(h("track", { kind: "subtitles", srclang: "en", label: "English", src: copy.subs_url }));
  $("player-wrap").replaceChildren(isVideo ? el : h("div", { class: "audio-player" }, art(e.thumb_url, "player-art"), el));
  $("sheet").classList.toggle("is-video", isVideo);
  $("sheet-title").textContent = e.title;
  $("sheet-sub").textContent = join(isVideo ? "Video" : "Audio", fmtDuration(e.duration), fmtBytes(copy.bytes));
  openSheet();
  el.play().catch(() => {});
}

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
  $("sheet").classList.remove("open");
  $("sheet").setAttribute("aria-hidden", "true");
  $("player-wrap").querySelector("audio, video")?.pause();
  setTimeout(() => { if (!$("sheet").classList.contains("open")) $("player-wrap").replaceChildren(); }, 400);
}
$("sheet-close").addEventListener("click", closeSheet);

// Swipe down to close the player (but leave the player's own controls alone).
let drag = null;
const sheetEl = $("sheet");
sheetEl.addEventListener("touchstart", (e) => {
  if (e.touches.length !== 1 || e.target.closest("audio, video")) return;
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

// ---- fast taps ------------------------------------------------------------
// iOS Safari swallows or delays taps for a moment after a list stops scrolling;
// native apps don't. Recognise taps ourselves and activate on touchend. A touch
// that lands while the list is still visibly moving only stops it, as in native
// apps. Speed matters, not timing: iOS keeps nudging a list by fractions of a
// pixel (and firing scroll events) well after it looks still, and taps then
// must still work.
const TAPPABLE = 'button, a[href], [role="switch"]';
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
  tapStart = { x: t.clientX, y: t.clientY, at: performance.now(), target: e.target, stoppingScroll: listStillMoving() };
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
  // Leave form controls and the player's own controls to iOS.
  if (tap.target.closest("input, select, textarea, label, audio, video")) return;
  const el = tap.target.closest(TAPPABLE);
  if (!el || el.disabled) return;
  // Links that leave the app (YouTube, Apple Podcasts) keep iOS's own handling.
  if (el.matches('a[target="_blank"], a[href^="podcast:"]')) return;
  e.preventDefault(); // skip Safari's own (delayed) click so it doesn't fire twice
  el.click();
}, { passive: false });

// iOS can ignore user-scalable=no; cancel its pinch gestures directly.
for (const type of ["gesturestart", "gesturechange", "gestureend"]) {
  document.addEventListener(type, (e) => e.preventDefault(), { passive: false });
}

// ---- navigation ---------------------------------------------------------------

// Show the compact nav title once the large title has scrolled under the nav bar.
function syncNav() {
  const navBottom = $("nav").getBoundingClientRect().bottom;
  $("nav").classList.toggle("scrolled", $("view-title").getBoundingClientRect().bottom <= navBottom + 4);
}
$("scroller").addEventListener("scroll", syncNav, { passive: true });

// Each page remembers how far it was scrolled, so going back returns you there.
const scrollMemory = new Map();

function onRoute({ restoreScroll = false } = {}) {
  expanded = null;
  confirmingRemove = null;
  render();
  $("scroller").scrollTop = restoreScroll ? (scrollMemory.get(location.hash) || 0) : 0;
  syncNav();
  load();
}

// Move to another page with an iOS-style push/pop slide. Uses view transitions
// where the browser supports them; elsewhere it simply switches.
let navToken = 0;
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

// In-app links replace the current history entry instead of adding one, so
// iOS's edge swipe has nothing to go "back" to (it would drag the app's pages
// sideways). The on-screen back button still works.
document.addEventListener("click", (e) => {
  const a = e.target.closest('a[href^="#/"]');
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  navigate(a.getAttribute("href"));
});
window.addEventListener("hashchange", () => onRoute());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") load();
});

// ---- boot -----------------------------------------------------------------

// The player version of Wren cached episodes on the phone through a service
// worker; remove it and its caches to give that space back.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations().then((regs) => regs.forEach((r) => r.unregister())).catch(() => {});
}
if ("caches" in window) {
  caches.keys().then((keys) => keys.forEach((k) => caches.delete(k))).catch(() => {});
}

document.querySelectorAll("[data-icon]").forEach((el) => el.prepend(icon(el.dataset.icon)));
onRoute();
