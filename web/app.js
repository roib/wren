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
  bookmark: '<path d="M7 4.5h10v15l-5-3.5-5 3.5z"/>',
  bell: '<path d="M6.5 16.5V11a5.5 5.5 0 0 1 11 0v5.5l1.5 2h-14z"/><path d="M10 20.5a2.2 2.2 0 0 0 4 0"/>',
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

// Days between a time and today, in this device's time zone (0 = today).
function daysAgo(ts) {
  return Math.round((new Date().setHours(0, 0, 0, 0) - new Date(ts * 1000).setHours(0, 0, 0, 0)) / 86400000);
}

// "7:42 AM", "Yesterday, 7:42 AM", "Oct 16, 7:42 AM": short, for a row.
function fmtWhen(ts) {
  const days = daysAgo(ts);
  if (days === 0) return fmtClock(ts);
  if (days === 1) return `Yesterday, ${fmtClock(ts)}`;
  return `${fmtDate(ymd(ts))}, ${fmtClock(ts)}`;
}

// "today at 7:42 AM", "Sun, Oct 19 at 7:42 AM": in a sentence.
function fmtWhenLong(ts) {
  const days = daysAgo(ts);
  const day = days === 0 ? "today" : days === 1 ? "yesterday" : days === -1 ? "tomorrow"
    : new Date(ts * 1000).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric",
      ...(new Date(ts * 1000).getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" }) });
  return `${day} at ${fmtClock(ts)}`;
}

const ymd = (ts) => {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
};

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
  if (/^(private|members-only) video$/.test(err)) return err[0].toUpperCase() + err.slice(1);
  if (/Private video/.test(err)) return "Private video";
  if (/unavailable|removed/i.test(err)) return "Video unavailable";
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
  if (s.kind === "book") return h("span", { class: "badges" }, h("span", { class: "badge" }, "Book"));
  return h("span", { class: "badges" },
    s.mode === "video" ? badge("video") : badge("audio"),
    s.mode === "audio" && s.video_copy ? badge("video") : null,
    s.enabled ? null : h("span", { class: "badge muted" }, "Paused"));
}

// ---- layout ------------------------------------------------------------------
// Wide windows get a desktop layout: a sidebar, the show list always visible,
// and the page (a show, Add, Alerts...) beside them. Narrower ones get the
// phone layout. Same pages, same code; the CSS switches at the same width.
const desktopMQ = matchMedia("(min-width: 1024px)");
const isDesktop = () => desktopMQ.matches;

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
  if (path === "/recent") return { name: "recent", id: "recent", params };
  if (path === "/add") return { name: "add", params };
  if (path === "/status") return { name: "status", params };
  if (path === "/alerts") return { name: "alerts", params };
  return { name: "home", params };
}

// How deep a page is: Shows is the top; everything else sits one level in.
function routeDepth(hash) {
  return !hash || hash === "#/" || hash === "#" ? 0 : 1;
}

async function load() {
  const r = route();
  try {
    const jobs = [];
    if (r.name === "show") jobs.push(api(`/api/shows/${r.id}/detail`).then((d) => details.set(r.id, d)));
    else if (r.name === "singles") jobs.push(api("/api/singles").then((d) => details.set("singles", d)));
    else if (r.name === "recent") jobs.push(api("/api/recent").then((d) => details.set("recent", d)));
    else if (r.name === "status") jobs.push(api("/api/activity").then((a) => { activity = a; }));
    else if (r.name === "add") jobs.push(api("/api/books").then((b) => { bookList = b.books; }));
    else if (r.name === "alerts") {
      jobs.push(api("/api/alerts").then(async (a) => { alertList = a; if (!pushState) await loadPushState(); }));
    }
    // Home and Status show the overview; on desktop the sidebar and show list always do.
    if (r.name === "home" || r.name === "status" || isDesktop()) {
      jobs.push(api("/api/overview").then((o) => { overview = o; }));
    }
    await Promise.all(jobs);
    showBadge(r.name === "alerts" ? alertList.active.length : overview?.alerts ?? 0);
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
  const data = ["show", "singles", "recent"].includes(r.name) ? details.get(r.id) : overview;
  pollTimer = setTimeout(() => {
    if (document.visibilityState === "visible") load();
    else schedule();
  }, isBusy(data) || (isDesktop() && isBusy(overview)) ? 4000 : 30000);
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
  // On desktop the show list is always there, so "Shows" opens the first show.
  if (isDesktop() && r.name === "home" && overview) {
    const first = overview.shows[0] ? `#/show/${overview.shows[0].id}` : overview.singles.episodes ? "#/singles" : null;
    if (first) {
      history.replaceState(null, "", first);
      onRoute();
      return;
    }
  }
  if (isDesktop()) renderPanes(r);
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
  } else if (r.name === "recent") {
    title = "Recent downloads";
    const d = details.get("recent");
    body = d ? recentView(d) : placeholder();
  } else if (r.name === "add") {
    title = "Add";
    body = addView(r.params);
  } else if (r.name === "alerts") {
    title = "Alerts";
    body = alertList ? alertsView(alertList) : placeholder();
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

// ---- desktop panes ---------------------------------------------------------------

let paneKind = "all"; // the show list's quick filter: all, audio, video, attention
const needsAttention = (s) => !!(s.last_error || s.failed || s.retrying);

function renderPanes(r) {
  renderSidebar(r);
  renderListPane(r);
}

function navLink(href, label, dot, count, active, countClass = "") {
  return h("a", { class: `side-link${active ? " active" : ""}`, href, "aria-current": active ? "page" : null },
    h("span", { class: "side-label" }, h("span", { class: "side-dot", style: `background: ${dot}` }), label),
    count ? h("span", { class: `side-count ${countClass}` }, String(count)) : null);
}

function renderSidebar(r) {
  const o = overview;
  const line = o ? workerLine(o) : null;
  $("sidebar").replaceChildren(
    h("div", { class: "brand" }, h("img", { src: "favicon.svg", alt: "", width: "28", height: "28" }), h("span", {}, "Wren")),
    h("a", { class: "button strong side-add", href: "#/add" }, icon("plus"), "Add show or episode"),
    h("nav", { class: "side-nav", "aria-label": "Wren" },
      navLink("#/", "Shows", "var(--fg)", o?.shows.length, r.name === "show" || r.name === "home"),
      navLink("#/recent", "Recent", "var(--muted)", o?.recent_count, r.name === "recent"),
      navLink("#/singles", "Single episodes", "var(--faint)", o?.singles.episodes, r.name === "singles"),
      navLink("#/alerts", "Alerts", "var(--warn)", o?.alerts, r.name === "alerts", "alert-count"),
      navLink("#/status", "Status", "var(--ok)", null, r.name === "status")),
    line ? h("a", { class: "side-status", href: "#/status" },
      h("span", { class: "side-status-main" },
        h("span", { class: `status-dot${line.busy ? " busy" : ""}${o.worker.paused_until ? " paused" : ""}` }), line.text),
      h("span", { class: "row-meta" }, line.next),
      h("span", { class: "row-meta" }, join(`${fmtBytes(o.disk.free)} free`, `yt-dlp ${o.worker.ytdlp}`))) : null);
}

function paneShows() {
  const q = filter.trim().toLowerCase();
  return (overview?.shows || []).filter((s) =>
    (!q || s.name.toLowerCase().includes(q))
    && (paneKind === "all"
      || (paneKind === "audio" && s.mode === "audio")
      || (paneKind === "video" && (s.mode === "video" || s.video_copy))
      || (paneKind === "attention" && needsAttention(s))));
}

function renderListPane(r) {
  const pane = $("listpane");
  // Built once, so the filter box keeps its focus and text across refreshes.
  if (!pane.dataset.built) {
    pane.dataset.built = "1";
    pane.append(
      h("div", { class: "pane-head" }, h("h2", {}, "Shows"), h("p", { id: "pane-sub", class: "row-meta" })),
      h("input", {
        id: "pane-filter", class: "filter", type: "search", placeholder: "Filter shows", autocomplete: "off",
        "aria-label": "Filter shows",
        oninput: (e) => { filter = e.target.value; renderListPane(route()); },
      }),
      h("div", { id: "pane-chips", class: "chips", role: "group", "aria-label": "Show filter" }),
      h("ul", { id: "pane-list", class: "pane-list" }),
      h("div", { id: "pane-other" }));
  }
  const o = overview;
  $("pane-sub").textContent = o ? join(plural(o.shows.length, "show"), plural(o.totals.episodes, "episode"), fmtBytes(o.totals.bytes)) : "";
  const attention = (o?.shows || []).filter(needsAttention).length;
  $("pane-chips").replaceChildren(...[["all", "All"], ["audio", "Audio"], ["video", "Video"],
    ["attention", attention ? `Needs attention · ${attention}` : "Needs attention"]].map(([kind, label]) =>
    h("button", {
      type: "button", class: `chip${paneKind === kind ? " on" : ""}${kind === "attention" && attention ? " warn" : ""}`,
      "aria-pressed": String(paneKind === kind),
      onclick: () => { paneKind = kind; renderListPane(route()); },
    }, label)));
  const shows = paneShows();
  $("pane-list").replaceChildren(...(shows.length
    ? shows.map((s) => {
      const li = showRow(s);
      if (r.name === "show" && r.id === s.id) li.querySelector(".row").classList.add("selected");
      return li;
    })
    : [h("li", { class: "none" }, o ? "No shows match." : "Loading…")]));
  $("pane-other").replaceChildren(...(o?.singles.episodes ? [
    h("h3", { class: "pane-section" }, "Other"),
    h("ul", { class: "pane-list" }, (() => {
      const li = singlesRow(o.singles);
      if (r.name === "singles") li.querySelector(".row").classList.add("selected");
      return li;
    })()),
  ] : []));
}

desktopMQ.addEventListener("change", () => { render(); load(); });

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
  else if (w.processing) {
    const step = { transcribing: "Transcribing", "finding ad reads": "Finding ad reads in",
      "waiting for Claude": "Waiting for Claude on the server for", cutting: "Removing sponsor reads from" };
    text = `${step[w.processing.step] || "Checking sponsor reads in"} “${w.processing.title}”`;
    busy = true;
  }
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
    data.recent.length ? section("Recently downloaded", h("ul", { class: "card list-card" },
      data.recent.map((e) => episodeRow(e, { withShow: true })),
      h("li", {}, h("a", { class: "row see-all", href: "#/recent" },
        h("span", { class: "row-text" }, "All recent downloads"), h("span", { class: "chev" }, icon("chevronRight")))))) : null,
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
  if (s.preparing) return h("span", { class: "row-status" }, h("span", { class: "spinner" }), `Finding chapters · ${s.preparing}`);
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
          h("span", {}, join(plural(s.episodes, s.kind === "book" ? "chapter" : "episode"),
            s.kind === "book" ? null : fmtDate(s.newest_date), fmtBytes(s.bytes)))),
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
  if (s.kind === "book") return bookHero(s);
  return h("div", { class: "hero card" },
    h("div", { class: "hero-top" },
      art(s.art_url, "hero-art"),
      h("div", { class: "hero-info" },
        showBadges(s),
        h("div", { class: "hero-meta" }, join(plural(s.episodes, "episode"), fmtBytes(s.bytes))),
        h("div", { class: "hero-meta" }, join(s.kind === "rss" ? "From its podcast feed" : "From YouTube",
          `checked ${ago(s.last_checked)}`, s.keep_last > 0 ? `keeps latest ${s.keep_last}` : "keeps everything")))),
    feedButtons(s.feed_path, "Open in Podcasts"),
    s.mode === "audio" && s.video_copy ? feedButtons(s.video_feed_path, "Open video feed") : null,
    h("div", { class: "hero-actions" },
      h("button", { class: "action", onclick: () => showCommand(s, "refresh", `Checking ${s.name} for new episodes…`) },
        icon("refresh"), "Check now"),
      h("button", { class: "action", onclick: () => showCommand(s, "download-now", `Downloading ${s.name} now…`) },
        icon("download"), "Download now"),
      h("a", { class: "action subtle", href: s.source_url, target: "_blank", rel: "noopener" }, icon("external"),
        s.kind === "rss" ? "Feed" : "YouTube")));
}

// A book doesn't get new episodes: no checks, no YouTube. Reading its folder
// again picks up a part added later.
function bookHero(s) {
  return h("div", { class: "hero card" },
    h("div", { class: "hero-top" },
      art(s.art_url, "hero-art"),
      h("div", { class: "hero-info" },
        showBadges(s),
        s.author ? h("div", { class: "hero-meta" }, `By ${s.author}`) : null,
        h("div", { class: "hero-meta" }, join(plural(s.episodes, "chapter"), fmtBytes(s.bytes))),
        s.preparing ? h("div", { class: "hero-meta" }, h("span", { class: "spinner" }), `Finding chapters · ${s.preparing}`) : null)),
    feedButtons(s.feed_path, "Open in Podcasts"),
    h("div", { class: "hero-actions" },
      h("button", { class: "action", onclick: () => showCommand(s, "refresh", `Reading ${s.name}’s files again…`) },
        icon("refresh"), "Look for new parts")));
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
      ? h("ul", { class: "card list-card" }, done.map((e) => episodeRow(e)))
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
        h("span", { class: "ep-meta" }, ...stateText(e),
          // ("Too short (53s)" already gives the length.)
          [fmtDate(e.upload_date), /^too short/.test(e.error) ? null : fmtDuration(e.duration)]
            .filter(Boolean).map((x) => ` · ${x}`).join(""))),
      canRetry ? h("button", { class: "action", onclick: () => onRetry(e) }, e.state === "failed" ? "Retry" : "Download") : null));
}

// withShow: name the show and give the download time (Recent, home), as the
// row isn't on its show's page. when: how to write that time.
function episodeRow(e, { withShow = false, when = fmtWhen } = {}) {
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
            e.copy_pending ? h("span", { class: "badge muted" }, "Video soon") : null,
            e.sponsors?.state === "pending" ? h("span", { class: "badge muted" }, "Not in feed yet") : null,
            // Processed, and something was cut (checked with nothing found gets no badge).
            e.sponsors?.state === "done" && e.sponsors.removed > 0
              ? h("span", { class: "badge cut", title: `${length(e.sponsors.removed)} removed` }, icon("check"), "Ads removed") : null),
          withShow
            ? h("span", {}, `${e.show} · `, h("span", { class: "when" }, icon("download"), when(e.added_at)),
              [fmtDuration(e.duration), fmtBytes(size)].filter(Boolean).map((x) => ` · ${x}`).join(""))
            : h("span", {}, join(fmtDate(e.upload_date), fmtDuration(e.duration), fmtBytes(size)))))),
    // Always rendered; collapsed with CSS until the row is open.
    h("div", { class: "collapse" },
      h("div", { class: "collapse-body" },
        h("p", { class: "ep-when" }, ...downloadedLines(e, { withShow })),
        cutsTable(e),
        chapterList(e),
        // Short labels: on phones these share one row, an icon over each label.
        h("div", { class: "ep-actions" },
          e.copies.map((c) => h("button", { class: "action", onclick: () => openPlayer(e, c) },
            icon("play"), e.copies.length > 1 ? (c.mode === "video" ? "Video" : "Audio") : "Play")),
          withShow ? h("a", { class: "action subtle", href: e.show_id ? `#/show/${e.show_id}` : "#/singles" },
            icon("chevronRight"), "Show") : null,
          e.source_url.startsWith("book:") ? null : h("a", { class: "action subtle", href: e.source_url, target: "_blank", rel: "noopener" },
            icon("external"), /youtube\.com|youtu\.be/.test(e.source_url) ? "YouTube" : "Web page"),
          h("button", { class: "action subtle danger", onclick: (ev) => deleteEpisode(e, ev.currentTarget) },
            icon("trash"), "Delete")))));
}

// When the episode came out and when the server got it (each copy, if they differ).
function downloadedLines(e, { withShow = false } = {}) {
  const times = e.copies.map((c) => c.added_at);
  const same = Math.max(...times) - Math.min(...times) < 120;
  const published = e.upload_date ? `Published ${fmtDate(e.upload_date)}` : null;
  const lines = [];
  // One line: when it came out and when it arrived (rows that name their show
  // already give the download time).
  if (same) lines.push(join(published, withShow ? null : `downloaded ${fmtWhenLong(times[0])}`));
  else {
    lines.push(published);
    for (const c of e.copies) lines.push(`${c.mode === "video" ? "Video" : "Audio"} downloaded ${fmtWhenLong(c.added_at)}`);
  }
  lines.push(sponsorLine(e.sponsors));
  return lines.filter(Boolean).flatMap((line, i) => (i ? [h("br"), line] : [line]));
}

// One line about sponsor reads; cuts that were made get a table instead (cutsTable).
function sponsorLine(sp) {
  if (!sp) return null;
  if (sp.state === "pending") {
    return sp.ready_at * 1000 <= Date.now() ? "Checking for sponsor reads · not in the feed yet"
      : `Waiting for sponsor marks · goes into the feed ${fmtWhenLong(sp.ready_at)}`;
  }
  // A transcript result set aside still leaves SponsorBlock's cuts (in the table).
  if (sp.note && !sp.cuts?.length) return `Sponsor reads ${sp.note}`;
  if (!sp.removed) return "No sponsor reads found";
  if (!sp.cuts?.length) return `Removed ${plural(sp.count || 1, "sponsor read")} · ${length(sp.removed)} in all`;
  return null;
}

const cutsShown = new Set(); // episodes whose cuts table is open (phones fold it away)

// What was cut: where (in the original video), how long, the advertiser and how it was found;
// and the lines taken out of the description.
function cutsTable(e) {
  const sp = e.sponsors;
  const lines = sp?.description_removed || [];
  const audio = Boolean(sp?.removed && sp.cuts?.length);
  if (sp?.state !== "done" || (!audio && !lines.length)) return null;
  const shown = cutsShown.has(e.video_id);
  const head = audio ? `Removed ${cutsSummary(sp.cuts, lines.length)} · ${length(sp.removed)} in all`
    : `Removed ${plural(lines.length, "line")} from the description`;
  return h("div", { class: `cuts${shown ? " shown" : ""}` },
    h("button", {
      class: "cuts-head", "aria-expanded": String(shown),
      onclick: (ev) => {
        const box = ev.currentTarget.closest(".cuts");
        const now = !box.classList.contains("shown");
        box.classList.toggle("shown", now);
        ev.currentTarget.setAttribute("aria-expanded", String(now));
        if (now) cutsShown.add(e.video_id); else cutsShown.delete(e.video_id);
      },
    }, head, h("span", { class: "chev" }, icon("chevronDown"))),
    audio && h("table", {},
      h("thead", {}, h("tr", {},
        h("th", {}, "Where"), h("th", {}, "Length"), h("th", {}, "Ad"), h("th", {}, "Source"))),
      h("tbody", {}, sp.cuts.map((c) => h("tr", {},
        h("td", { class: "num" }, `${clock(c.start)}–${clock(c.end)}`),
        h("td", { class: "num" }, length(c.end - c.start)),
        h("td", {}, c.category ? (c.category === "intro" ? "Intro" : "Outro")
          : c.kind === "promo" ? `Promo · ${c.advertiser || "—"}` : c.advertiser || "—"),
        h("td", { class: "src" }, c.source === "transcript" ? "Transcript" : "SponsorBlock"))))),
    audio && h("div", { class: "cuts-note" }, "Times are in the original video."),
    lines.length > 0 && h("div", { class: "cuts-desc" },
      h("div", { class: "cuts-desc-head" }, "From the description"),
      h("ul", {}, lines.map((l) => h("li", {}, l)))));
}

const chaptersShown = new Set(); // episodes whose chapter list is open

// The chapters written into the episode, at their times in the cut audio.
function chapterList(e) {
  const sp = e.sponsors;
  if (!sp?.chapters?.length) return null;
  const shown = chaptersShown.has(e.video_id);
  const from = sp.chapters_source === "creator" ? "the show's own" : "made from the transcript";
  return h("div", { class: `cuts chapters${shown ? " shown" : ""}` },
    h("button", {
      class: "cuts-head", "aria-expanded": String(shown),
      onclick: (ev) => {
        const box = ev.currentTarget.closest(".cuts");
        const now = !box.classList.contains("shown");
        box.classList.toggle("shown", now);
        ev.currentTarget.setAttribute("aria-expanded", String(now));
        if (now) chaptersShown.add(e.video_id); else chaptersShown.delete(e.video_id);
      },
    }, `${plural(sp.chapters.length, "chapter")} · ${from}`, h("span", { class: "chev" }, icon("chevronDown"))),
    h("table", {}, h("tbody", {}, sp.chapters.map((c) => h("tr", {},
      h("td", { class: "num" }, clock(c.start)), h("td", {}, c.title))))));
}

// "2 sponsor reads and the intro", "1 sponsor read, 2 promos and the intro and outro", "the intro",
// "1 promo and 3 description lines".
function cutsSummary(cuts, descriptionLines = 0) {
  const reads = cuts.filter((c) => !c.category && c.kind !== "promo").length;
  const promos = cuts.filter((c) => !c.category && c.kind === "promo").length;
  const extras = ["intro", "outro"].filter((k) => cuts.some((c) => c.category === k));
  const parts = [reads ? plural(reads, "sponsor read") : null, promos ? plural(promos, "promo") : null,
    extras.length ? `the ${extras.join(" and ")}` : null,
    descriptionLines ? plural(descriptionLines, "description line") : null].filter(Boolean);
  return parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}` : parts.join("");
}

// A length of time that can't be mistaken for a clock time: "44s", "1m 21s", "1h 02m".
function length(sec) {
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${String(sec % 60).padStart(2, "0")}s`;
  return `${Math.floor(sec / 3600)}h ${String(Math.floor((sec % 3600) / 60)).padStart(2, "0")}m`;
}

// Seconds as m:ss, or h:mm:ss past an hour.
function clock(sec) {
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = String(sec % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
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
    settingsCard(s, d.worker));
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

function settingsCard(s, w) {
  const confirming = confirmingRemove === s.id;
  if (s.kind === "book") {
    return section("Settings", h("div", { class: "settings card" },
      settingRow("Keep", h("span", { class: "setting-value" }, "Every chapter"),
        "Removing the book deletes its chapters from Wren; its files in the books folder stay."),
      h("button", { class: `remove${confirming ? " confirm" : ""}`, onclick: () => removeShow(s) },
        confirming ? "Tap again to remove" : "Remove book")));
  }
  return section("Settings", h("div", { class: "settings card" },
    settingRow("Source", h("a", { class: "action subtle", href: `#/add?q=${encodeURIComponent(s.name)}` }, "Change"),
      s.kind === "rss" ? "Its podcast feed. To switch back, paste the show's YouTube link on the Add page."
        : "YouTube. To use its podcast feed instead (audio only, no YouTube limits), find it on the Add page."),
    s.kind === "rss" ? null : settingRow("Format", h("div", { class: "segmented small", role: "radiogroup", "aria-label": "Format" },
      ["audio", "video"].map((m) => h("label", {},
        h("input", {
          type: "radio", name: `mode-${s.id}`, value: m, checked: s.mode === m,
          onchange: () => patchShow(s, { mode: m }, `New episodes will download as ${m}`),
        }),
        h("span", {}, m === "audio" ? "Audio" : "Video")))),
      "Applies to episodes downloaded from now on."),
    s.mode === "audio" && s.kind !== "rss" ? settingRow("Also download video",
      toggle(!!s.video_copy, (on) => patchShow(s, { video_copy: on },
        on ? `Downloading ${s.name} as video too` : "Video copies off"), "Also download video"),
      "Adds a separate video feed alongside the audio one.") : null,
    settingRow("Remove sponsor reads", toggle(!!s.skip_sponsors, (on) => patchShow(s, { skip_sponsors: on },
      on ? "Sponsor reads will be removed from new episodes" : "Sponsor reads kept"), "Remove sponsor reads"),
      w.sponsor_mode === "transcript"
        ? "New episodes are checked right after downloading: the sponsor reads found in a transcript, and any marked "
          + "on SponsorBlock, are cut out before they go into the feed. Episodes already downloaded stay as they are."
        : `New episodes wait until ${w.sponsor_delay_hours} hours after they're published, then the sponsor reads `
          + "SponsorBlock users marked are cut out. Episodes already downloaded stay as they are."),
    s.skip_sponsors && s.kind !== "rss" ? settingRow("Also cut intro and outro",
      toggle(!!s.skip_intros, (on) => patchShow(s, { skip_intros: on },
        on ? "Intros and outros will be cut from new episodes" : "Intros and outros kept"), "Also cut intro and outro"),
      "The show's opening and closing (theme, music), where SponsorBlock users have marked them.") : null,
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
    toast(s.kind === "book" ? `Removed ${s.name}.` : `Removed ${s.name}. Its episodes moved to Single episodes.`, 5000);
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
        art(d.art_url, "hero-art"),
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

// ---- recent downloads -----------------------------------------------------------

// "Today", "Yesterday", or the date, for when an episode was downloaded.
function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

function recentView(d) {
  if (!d.entries.length) return h("p", { class: "none card" }, "Nothing downloaded yet.");
  const days = new Map();
  for (const e of d.entries) {
    const day = dayLabel(e.added_at);
    if (!days.has(day)) days.set(day, []);
    days.get(day).push(e);
  }
  return h("div", {},
    [...days].map(([day, entries]) => section(day,
      h("ul", { class: "card list-card" }, entries.map((e) => episodeRow(e, { withShow: true, when: fmtClock }))))),
    h("p", { class: "page-note" }, "Episodes removed to keep a show's latest few no longer appear here."));
}

// ---- add ------------------------------------------------------------------------

const add = { url: "", shared: null, busy: false, result: null, error: null, epMode: "audio", subs: {} };
let bookList = []; // what's in the server's books folder

function addView(params) {
  // A link from the "Share to Wren" shortcut: look it up straight away.
  const shared = params.get("url") || params.get("q");
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
          id: "add-url", type: "text", value: add.url, placeholder: "YouTube link, podcast feed or show name",
          autocomplete: "off", autocapitalize: "off", spellcheck: "false",
          oninput: (e) => { add.url = e.target.value; },
        }),
        canPaste ? h("button", { type: "button", class: "action", onclick: pasteLink }, "Paste") : null),
      h("button", { type: "submit", class: "primary", disabled: add.busy }, add.busy ? "Looking up…" : "Look up")),
    add.error ? banner(add.error) : null,
    add.result ? resultView(add.result) : null,
    add.result ? null : booksSection(),
    bookmarklet(),
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
const subChoice = (url) => (add.subs[url] ??= { mode: "audio", backfill: 3, skipSponsors: false, skipIntros: false });

function subscribeBlock(url, name, subscribed, { audioOnly = false, sameShow = [], podcastFeed = false } = {}) {
  if (subscribed) {
    return h("a", { class: "row subscribed", href: `#/show/${subscribed.id}` },
      icon("check"), h("span", { class: "row-text" }, `Already subscribed to ${subscribed.name}`),
      h("span", { class: "chev" }, icon("chevronRight")));
  }
  const choice = subChoice(url);
  if (audioOnly) choice.mode = "audio";
  // Following the same show from YouTube and its feed would download every
  // episode twice: offer to move the existing show over instead.
  const switches = sameShow.map((s) => h("div", { class: "switch-offer" },
    h("p", {}, `You follow ${s.name} from ${s.kind === "rss" ? "its podcast feed" : "YouTube"}. Switch it to `
      + `${podcastFeed ? "this podcast feed" : "YouTube"}? It keeps its episodes, settings and feed in Podcasts.`),
    h("button", { class: "primary", onclick: (e) => switchSource(s, url, e.currentTarget) },
      `Switch ${s.name} to ${podcastFeed ? "the podcast feed" : "YouTube"}`)));
  return h("div", { class: "choice" },
    ...switches,
    // The rest is for adding it as a new show, which a switch makes unusual.
    switches.length ? h("div", { class: "field-label or" }, "Or add it as a separate show") : null,
    audioOnly ? null : [h("div", { class: "field-label" }, "Format"), modePicker(choice, "mode", `mode-${url}`)],
    h("div", { class: "field-label" }, "Download the latest"), backfillPicker(choice, `backfill-${url}`),
    // The same switches as in show settings, so the first episodes are cut too.
    h("div", { class: "sub-settings" },
      settingRow("Remove sponsor reads", toggle(choice.skipSponsors, (on) => { choice.skipSponsors = on; render(); },
        "Remove sponsor reads")),
      choice.skipSponsors && !podcastFeed ? settingRow("Also cut intro and outro",
        toggle(choice.skipIntros, (on) => { choice.skipIntros = on; }, "Also cut intro and outro")) : null),
    h("button", { class: switches.length ? "button full" : "primary", onclick: (e) => subscribe(url, name, e.currentTarget) },
      switches.length ? "Add as a separate show" : `Subscribe to ${name}`));
}

async function switchSource(show, url, btn) {
  btn.disabled = true;
  try {
    const updated = await api(`/api/shows/${show.id}/source`, { method: "POST", body: { url } });
    toast(`${updated.name} now comes from ${updated.kind === "rss" ? "its podcast feed" : "YouTube"}. New episodes download from there.`, 5000);
    Object.assign(add, { url: "", result: null, error: null, subs: {} });
    navigate(`#/show/${show.id}`);
  } catch (err) {
    btn.disabled = false;
    toast(`Couldn't switch: ${err.message}`, 6000);
  }
}

// Audiobooks put in the books folder on the server, ready to add.
function booksSection() {
  if (!bookList.length) return null;
  return section("Books folder", h("ul", { class: "card list-card" }, bookList.map((b) => h("li", {},
    b.added
      ? h("a", { class: "row", href: `#/show/${b.added.id}` },
        h("span", { class: "row-text" }, h("span", { class: "row-title" }, b.name),
          h("span", { class: "row-meta" }, join("Added", b.parts > 1 ? plural(b.parts, "file") : null, fmtBytes(b.bytes)))),
        h("span", { class: "chev" }, icon("chevronRight")))
      : h("button", { class: "row", onclick: () => { add.url = b.source; lookup(); } },
        h("span", { class: "row-text" }, h("span", { class: "row-title" }, b.name),
          h("span", { class: "row-meta" }, join(b.parts > 1 ? plural(b.parts, "file") : null, fmtBytes(b.bytes)))),
        h("span", { class: "chev" }, icon("chevronRight")))))));
}

const PREVIEW_CHAPTERS = 5;

function bookResult(r) {
  const more = r.chapters.length - PREVIEW_CHAPTERS;
  return section("Book", h("div", { class: "card result" },
    h("div", { class: "result-video" },
      art(r.thumb_url, "result-thumb"),
      h("div", { class: "row-text" },
        h("div", { class: "result-title" }, r.title),
        h("div", { class: "row-meta" }, join(r.author, plural(r.chapters.length, "chapter"), fmtDuration(r.duration))))),
    // Its chapters will be found in the audio from the book's text: Whisper takes about 1/40 of its length.
    r.placing ? h("p", { class: "setting-hint" }, `Chapters from ${r.epub}: Wren transcribes the audio and finds where `
      + `each chapter begins (about ${Math.max(1, Math.round(r.duration / 40 / 60))} min) before cutting them.`) : null,
    h("ul", { class: "preview" }, r.chapters.slice(0, PREVIEW_CHAPTERS).map((c) => h("li", {},
      h("span", { class: "preview-title" }, c.title),
      h("span", { class: "preview-meta" }, fmtDuration(c.duration) || ""))),
      more > 0 ? h("li", {}, h("span", { class: "preview-title" }, `and ${plural(more, "more chapter")}`)) : null),
    r.subscribed
      ? h("a", { class: "row subscribed", href: `#/show/${r.subscribed.id}` },
        icon("check"), h("span", { class: "row-text" }, `Already added as ${r.subscribed.name}`),
        h("span", { class: "chev" }, icon("chevronRight")))
      : h("button", { class: "primary", onclick: (e) => addBook(r, e.currentTarget) }, "Add book")));
}

async function addBook(r, btn) {
  btn.disabled = true;
  btn.textContent = "Adding…";
  try {
    const show = await api("/api/shows", { method: "POST", body: { url: r.url } });
    toast(r.placing ? `Added ${show.name}. Its chapters appear once Wren has found them in the audio.`
      : `Added ${show.name}. Its ${plural(r.chapters.length, "chapter")} are being cut into episodes.`, 5000);
    Object.assign(add, { url: "", result: null, error: null, subs: {} });
    navigate(`#/show/${show.id}`);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Add book";
    toast(`Couldn't add: ${err.message}`, 6000);
  }
}

function resultView(r) {
  if (r.kind === "book") return bookResult(r);
  if (r.kind === "search") {
    return section("Podcasts", r.results.length
      ? h("ul", { class: "card list-card" }, r.results.map((p) => h("li", {},
        h("button", { class: "row", onclick: () => { add.url = p.feed_url; lookup(); } },
          art(p.image),
          h("span", { class: "row-text" },
            h("span", { class: "row-title" }, p.title),
            h("span", { class: "row-meta" }, join(p.author, p.subscribed ? "Following" : null))),
          h("span", { class: "chev" }, icon("chevronRight"))))))
      : h("p", { class: "none card" }, `No podcasts found for “${r.query}”. Paste a YouTube link or a feed address instead.`));
  }
  if (r.kind === "show") {
    const podcastFeed = r.source === "rss";
    return section(podcastFeed ? "Podcast feed" : "Show", h("div", { class: "card result" },
      h("div", { class: "result-title" }, r.title),
      r.latest.length ? h("ul", { class: "preview" }, r.latest.map((i) => h("li", {},
        h("span", { class: "preview-title" }, i.title),
        h("span", { class: "preview-meta" }, i.live ? "Live" : fmtDuration(i.duration) || "")))) : null,
      subscribeBlock(r.url, r.title, r.subscribed, { audioOnly: podcastFeed, sameShow: r.same_show || [], podcastFeed })));
  }
  return h("div", { class: "results" },
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
    const { mode, backfill, skipSponsors, skipIntros } = subChoice(url);
    const show = await api("/api/shows", { method: "POST",
      body: { url, mode, backfill, skip_sponsors: skipSponsors, skip_intros: skipSponsors && skipIntros } });
    toast(`Subscribed to ${show.name}. Downloading the latest ${backfill}`
      + `${show.skip_sponsors ? ", with sponsor reads removed" : ""}.`, 5000);
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

// Desktop: a bookmarklet that opens Wren's Add page for the current tab.
function bookmarklet() {
  const code = `javascript:(()=>{window.open(${JSON.stringify(`${location.origin}/#/add?url=`)}+encodeURIComponent(location.href),"_blank")})()`;
  return h("section", { class: "section desktop-only" }, h("div", { class: "bookmarklet card" },
    h("div", { class: "row-text" },
      h("span", { class: "row-title" }, "Add from any YouTube tab"),
      h("span", { class: "row-meta wrap" },
        "Drag this button to your bookmarks bar. Click it on a YouTube page and Wren opens with the link looked up.")),
    h("a", {
      class: "action", href: code, draggable: "true",
      onclick: (e) => { e.preventDefault(); toast("Drag it to your bookmarks bar"); },
    }, icon("bookmark"), "Add to Wren")));
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

// ---- download activity ------------------------------------------------------------
// One row per day (today on top), the day's hours left to right, a dot for each
// download when it finished: hollow for audio, filled for video, like the badges.
// Drawn in the server's time zone, so it lines up with its download hours.

let activity = null; // /api/activity
let activityPick = null; // download log id of the dot whose details are showing

function zoned(ts, timeZone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(ts * 1000)).map((p) => [p.type, p.value]));
  return { day: `${parts.year}-${parts.month}-${parts.day}`, hour: Number(parts.hour) + Number(parts.minute) / 60 };
}

function hourLabel(n) {
  return n % 12 === 0 ? (n % 24 === 0 ? "12a" : "12p") : n < 12 ? `${n}a` : `${n - 12}p`;
}

function activityChart(a, w) {
  const tz = w.timezone;
  const pct = (hour) => `${(hour / 24) * 100}%`;
  // The last `a.days` days in the server's time zone, today first.
  const [y, m, d] = zoned(Date.now() / 1000, tz).day.split("-").map(Number);
  const days = Array.from({ length: a.days }, (_, i) => new Date(Date.UTC(y, m - 1, d - i)));
  const key = (date) => date.toISOString().slice(0, 10);
  const byDay = new Map(days.map((date) => [key(date), []]));
  for (const dl of a.downloads) byDay.get(zoned(dl.at, tz).day)?.push(dl);

  // The download hours, as one band (or two, when they wrap past midnight).
  const [from, to] = w.active_hours;
  const bands = from === to || (from === 0 && to === 24) ? [[0, 24]] : from < to ? [[from, to]] : [[0, to], [from, 24]];

  const detail = h("p", { class: "act-detail", "aria-live": "polite" });
  const when = (ts) => new Date(ts * 1000).toLocaleString([], { timeZone: tz, weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  // One download, or a group that arrived together (podcast feeds download back to back).
  const describe = (group) => {
    if (group.length === 1) {
      const dl = group[0];
      detail.replaceChildren(h("strong", {}, dl.title), h("br"), join(dl.show, dl.mode === "video" ? "Video" : "Audio", when(dl.at), fmtBytes(dl.bytes)));
      return;
    }
    detail.replaceChildren(h("strong", {}, `${group.length} downloads at ${when(group[0].at)}`),
      ...group.flatMap((dl) => [h("br"), join(dl.title, dl.show, dl.mode === "video" ? "Video" : null)]));
  };
  const groups = new Map(); // a download log id -> the group it's in
  // Downloads closer together than this share a spot, so their dots would
  // cover each other: draw them as one badge with their count.
  const GROUP_HOURS = 20 / 60;
  const group = (downloads) => {
    const out = [];
    for (const dl of [...downloads].sort((p, q) => p.at - q.at)) {
      const last = out.at(-1);
      if (last && (dl.at - last.at(-1).at) / 3600 < GROUP_HOURS) last.push(dl);
      else out.push([dl]);
    }
    for (const g of out) for (const dl of g) groups.set(dl.id, g);
    return out;
  };
  const dayGroups = new Map(days.map((date) => [key(date), group(byDay.get(key(date)))]));
  const picked = a.downloads.find((dl) => dl.id === activityPick);
  if (picked) describe(groups.get(picked.id) || [picked]);
  else detail.textContent = isDesktop() ? "Point at a dot to see what was downloaded." : "Tap a dot to see what was downloaded.";

  const dot = (g) => h("button", {
    type: "button",
    class: `act-dot ${g.length > 1 ? `group ${g.every((dl) => dl.mode === "video") ? "video" : "audio"}` : g[0].mode}`
      + (g.some((dl) => dl.id === activityPick) ? " picked" : ""),
    style: `left: ${pct(zoned(g[0].at, tz).hour + (g.at(-1).at - g[0].at) / 7200)}`,
    "aria-label": g.length > 1 ? `${g.length} downloads at ${fmtClock(g[0].at)}` : `${g[0].title}, ${g[0].show}, ${fmtClock(g[0].at)}`,
    onclick: (ev) => {
      activityPick = g[0].id;
      ev.currentTarget.closest(".act").querySelectorAll(".act-dot.picked").forEach((x) => x.classList.remove("picked"));
      ev.currentTarget.classList.add("picked");
      describe(g);
    },
    onmouseenter: () => describe(g),
  }, g.length > 1 ? h("span", { class: "count" }, String(g.length)) : null);

  const total = a.downloads.length;
  return h("div", { class: "card act" },
    h("div", { class: "act-head" },
      h("span", {}, total ? `${plural(total, "download")} in the last ${a.days} days` : `No downloads in the last ${a.days} days`),
      h("span", { class: "act-legend" },
        h("span", { class: "act-key audio" }), "Audio", h("span", { class: "act-key video" }), "Video",
        h("span", { class: "act-key band" }), "Download hours")),
    h("div", { class: "act-grid" },
      days.map((date, i) => {
        const label = i === 0 ? "Today" : i === 1 ? "Yesterday"
          : `${date.toLocaleDateString(undefined, { timeZone: "UTC", weekday: "short" })} ${date.getUTCDate()}`;
        return h("div", { class: "act-row" },
          h("span", { class: "act-day" }, label),
          h("div", { class: "act-track" },
            bands.map(([b0, b1]) => h("span", { class: "act-band", style: `left: ${pct(b0)}; width: ${pct(b1 - b0)}` })),
            dayGroups.get(key(date)).map(dot)));
      }),
      h("div", { class: "act-row act-axis", "aria-hidden": "true" },
        h("span", { class: "act-day" }),
        h("div", { class: "act-track" }, [0, 4, 8, 12, 16, 20, 24].map((n) =>
          h("span", { class: "act-tick", style: `left: ${pct(n)}` }, hourLabel(n)))))),
    detail);
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
      w.paused_until ? kv("Paused until", fmtClock(w.paused_until)) : null,
      kv("yt-dlp", w.ytdlp)),
      h("div", { class: "hero-actions" },
        h("button", { class: "action", onclick: () => command("/api/refresh", "Checking every show…") }, icon("refresh"), "Check all now"),
        h("button", { class: "action", onclick: () => command("/api/download-now", "Downloading everything queued now…") },
          icon("download"), "Download all now"))),
    activity ? section("Download activity", activityChart(activity, w)) : null,
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

// ---- alerts page -------------------------------------------------------------------

let alertList = null; // /api/alerts: { active, recent }

// The count on the bell and on the home-screen icon.
function showBadge(n) {
  $("alerts-badge").hidden = !n;
  $("alerts-badge").textContent = n > 9 ? "9+" : String(n || "");
  $("alerts-link").setAttribute("aria-label", n ? `Alerts: ${n}` : "Alerts");
  try {
    if (n) navigator.setAppBadge?.(n)?.catch?.(() => {});
    else navigator.clearAppBadge?.()?.catch?.(() => {});
  } catch {}
}

function alertRow(a, active) {
  const when = active ? ago(a.created_at) : `Resolved ${ago(a.resolved_at)}`;
  return h("li", { class: `alert-item${active ? "" : " resolved"}` },
    h("a", { class: "alert-main", href: a.url.replace(/^\//, "") || "#/" },
      h("span", { class: "alert-icon" }, icon(active ? "alert" : "check")),
      h("span", { class: "row-text" },
        h("span", { class: "alert-title" }, a.title),
        h("span", { class: "alert-body" }, a.body),
        h("span", { class: "row-meta" }, when))),
    active ? h("button", { class: "action subtle", onclick: () => dismissAlert(a.id) }, "Dismiss") : null);
}

function alertsView(list) {
  return h("div", {},
    section(list.active.length > 1 ? null : "Now",
      list.active.length > 1 ? h("div", { class: "section-head" },
        h("h2", {}, "Now"),
        h("button", { class: "action subtle", onclick: () => dismissAlert(null) }, "Clear all")) : null,
      list.active.length
        ? h("ul", { class: "card list-card" }, list.active.map((a) => alertRow(a, true)))
        : h("p", { class: "none card" }, "Nothing needs your attention.")),
    list.recent.length ? section("Earlier", h("ul", { class: "card list-card" }, list.recent.map((a) => alertRow(a, false)))) : null,
    notificationsSection());
}

async function dismissAlert(id) {
  try {
    await api(id === null ? "/api/alerts/dismiss-all" : `/api/alerts/${id}/dismiss`, { method: "POST" });
  } catch (err) {
    toast(`Couldn't dismiss: ${err.message}`);
  }
  load();
}

// ---- alerts (Web Push) ------------------------------------------------------------

// Whether this device can get alerts and has them on: { supported, permission, subscribed }.
let pushState = null;

async function loadPushState() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    pushState = { supported: false };
    return;
  }
  try {
    const reg = await Promise.race([navigator.serviceWorker.ready, new Promise((_, no) => setTimeout(no, 4000))]);
    const sub = await reg.pushManager.getSubscription();
    // Re-register it with the server, in case the server forgot it.
    if (sub) api("/api/push/subscribe", { method: "POST", body: sub.toJSON() }).catch(() => {});
    pushState = { supported: true, permission: Notification.permission, subscribed: !!sub };
  } catch {
    pushState = { supported: false };
  }
}

function base64UrlToBytes(s) {
  const b = atob(s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "="));
  return Uint8Array.from(b, (c) => c.charCodeAt(0));
}

async function enableAlerts(btn) {
  btn.disabled = true;
  try {
    // Must be the first thing after the tap: iOS only asks from a user gesture.
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error("notifications weren't allowed");
    const reg = await navigator.serviceWorker.ready;
    const { key } = await api("/api/push/key");
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64UrlToBytes(key) });
    await api("/api/push/subscribe", { method: "POST", body: sub.toJSON() });
    toast("Alerts are on");
  } catch (err) {
    // Chromium builds without Google's API keys (common on Linux), and Brave
    // with Google push turned off, can't reach a push service at all.
    toast(/push service/i.test(err.message)
      ? "This browser can't receive push notifications. Use Chrome, Firefox or Safari, or in Brave turn on "
        + "“Use Google services for push messaging”."
      : `Couldn't turn on notifications: ${err.message}`, 9000);
  }
  await loadPushState();
  render();
}

async function disableAlerts() {
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await api("/api/push/unsubscribe", { method: "POST", body: { endpoint: sub.endpoint } });
      await sub.unsubscribe();
    }
    toast("Alerts are off");
  } catch (err) {
    toast(`Couldn't turn off alerts: ${err.message}`);
  }
  await loadPushState();
  render();
}

async function testAlert() {
  try {
    await api("/api/push/test", { method: "POST" });
    toast("Test alert sent");
  } catch (err) {
    toast(`Couldn't send: ${err.message}`);
  }
}

const ALERTS_ABOUT = "Notifications let you know right away when YouTube blocks downloads, an episode fails for good, a show can't be "
  + "checked or goes quiet, space runs low, or yt-dlp couldn't update.";

function notificationsSection() {
  const p = pushState;
  let body;
  if (!p) body = h("p", { class: "none" }, "Checking…");
  else if (!p.supported) {
    body = h("p", { class: "none" }, "Open Wren from your home screen to get notifications.");
  } else if (p.permission === "denied") {
    body = h("p", { class: "none" }, "Notifications are blocked. Allow them in iOS Settings → Notifications → Wren.");
  } else if (p.subscribed) {
    body = [
      kv("Notifications", "On for this device"),
      h("div", { class: "hero-actions pad" },
        h("button", { class: "action", onclick: testAlert }, "Send a test alert"),
        h("button", { class: "action subtle", onclick: disableAlerts }, "Turn off")),
    ];
  } else {
    body = h("div", { class: "pad" },
      h("button", { class: "button strong full", "data-native-tap": true, onclick: (e) => enableAlerts(e.currentTarget) },
        "Turn on notifications"));
  }
  return section("Notifications", h("div", { class: "card list-card" }, body), h("p", { class: "setting-hint section-hint" }, ALERTS_ABOUT));
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
  $("sheet-backdrop").hidden = false;
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
  $("sheet-backdrop").hidden = true;
  $("player-wrap").querySelector("audio, video")?.pause();
  setTimeout(() => { if (!$("sheet").classList.contains("open")) $("player-wrap").replaceChildren(); }, 400);
}
$("sheet-close").addEventListener("click", closeSheet);
$("sheet-backdrop").addEventListener("click", closeSheet);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("sheet").classList.contains("open")) closeSheet();
});

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
  // Links that leave the app (YouTube, Apple Podcasts) and buttons that ask iOS
  // for a permission keep iOS's own handling.
  if (el.matches('a[target="_blank"], a[href^="podcast:"], [data-native-tap]')) return;
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
  // Desktop swaps the page beside the list, so a quick fade rather than a slide.
  const dir = isDesktop() ? "fade" : to > from ? "forward" : to < from ? "back" : "fade";
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

// The service worker delivers alerts (and clears what the old player version
// saved on the phone).
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});

document.querySelectorAll("[data-icon]").forEach((el) => el.prepend(icon(el.dataset.icon)));
onRoute();
