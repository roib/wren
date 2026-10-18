// Wren no longer works offline (playback moved to podcast apps). This worker
// replaces the old caching one on phones that still have it: it deletes the
// caches, including episodes saved to the device, and unregisters itself.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const key of await caches.keys()) await caches.delete(key);
    await self.registration.unregister();
    for (const client of await self.clients.matchAll()) client.navigate(client.url);
  })());
});
