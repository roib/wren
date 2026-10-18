// Service worker:
// - app shell: network first, cache fallback (so the app opens offline)
// - /api/library: network first, cache fallback (episode list works offline)
// - /media/*: served from the media cache when downloaded, with Range support
//   (Safari requires 206 responses to play/seek audio and video)
const SHELL = "wren-shell-v3";
const DATA = "wren-data-v1";
const MEDIA = "wren-media-v1"; // must match app.js
const SHELL_FILES = [
  "/", "/index.html", "/app.js", "/style.css", "/manifest.webmanifest", "/icon-180.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(Promise.all([
    caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)),
    caches.open(DATA).then((c) => c.add("/api/library")).catch(() => {}),
  ]));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  const keep = [SHELL, DATA, MEDIA];
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => !keep.includes(k)).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;

  if (url.pathname.startsWith("/media/")) {
    e.respondWith(serveMedia(e.request, url));
  } else if (url.pathname === "/api/library") {
    e.respondWith(networkFirst(e.request, DATA));
  } else if (url.pathname.startsWith("/api/")) {
    return;
  } else {
    e.respondWith(networkFirst(e.request, SHELL));
  }
});

async function networkFirst(request, cacheName) {
  try {
    // Always check with the server, so an update never mixes old and new files.
    const res = await fetch(request, { cache: "no-cache" });
    if (res.ok) {
      const copy = res.clone();
      caches.open(cacheName).then((c) => c.put(request, copy));
    }
    return res;
  } catch {
    const cached = await caches.match(request);
    return cached || Response.error();
  }
}

async function serveMedia(request, url) {
  const cache = await caches.open(MEDIA);
  const cached = await cache.match(url.pathname);

  if (!cached) {
    // Keep thumbnails and subtitles around so they work offline too.
    if (url.pathname.endsWith(".jpg") || url.pathname.endsWith(".vtt")) {
      try {
        const res = await fetch(request);
        if (res.ok) cache.put(url.pathname, res.clone());
        return res;
      } catch {
        return Response.error();
      }
    }
    return fetch(request);
  }

  const range = request.headers.get("Range");
  if (!range) return cached;

  const blob = await cached.blob();
  const size = blob.size;
  const m = /^bytes=(\d*)-(\d*)$/.exec(range.trim());
  let start;
  let end;
  if (m && m[1] === "" && m[2] !== "") {
    start = Math.max(0, size - Number(m[2])); // suffix range: last N bytes
    end = size - 1;
  } else if (m && m[1] !== "") {
    start = Number(m[1]);
    end = m[2] === "" ? size - 1 : Math.min(Number(m[2]), size - 1);
  }
  if (start === undefined || start > end) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
  }

  return new Response(blob.slice(start, end + 1), {
    status: 206,
    headers: {
      "Content-Type": cached.headers.get("Content-Type") || blob.type,
      "Content-Range": `bytes ${start}-${end}/${size}`,
      "Content-Length": String(end - start + 1),
      "Accept-Ranges": "bytes",
    },
  });
}
