const CACHE = "cockpit-shell-v8";
const SHELL = ["/", "/manifest.json", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  if (url.pathname === "/state") return; // always live, never cached
  // Materialien und API nie cachen (große Dateien, Gesundheitsdaten, immer aktuell)
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/files/")) return;

  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (!res.ok) return res; // keine 401/Fehlerseiten cachen
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
