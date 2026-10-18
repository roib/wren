// Service worker: only for alerts (Web Push). Wren doesn't work offline, so
// there's no fetch handler; the caches the old player version kept on the
// phone (episodes saved for offline listening) are deleted.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const key of await caches.keys()) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("push", (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch { data = { body: e.data?.text() }; }
  if (typeof data.badge === "number") {
    try { data.badge ? self.navigator.setAppBadge?.(data.badge) : self.navigator.clearAppBadge?.(); } catch {}
  }
  e.waitUntil(self.registration.showNotification(data.title || "Wren", {
    body: data.body || "",
    tag: data.tag || undefined,
    icon: "/icon-192.png?v=bird",
    data: { url: data.url || "/" },
  }));
});

// Tapping an alert opens Wren on the page it's about.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of windows) {
      if ("focus" in w) {
        await w.focus();
        if ("navigate" in w) await w.navigate(url).catch(() => {});
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
