/* RouteForge service worker.
   Caches the app shell so the interface opens instantly and survives a flaky
   connection. It deliberately does NOT cache API responses: routing needs a
   live server, and a stale plan would be worse than an honest error. */

const CACHE = "routeforge-shell-v1";
const SHELL = [
  "/",
  "/static/app.css",
  "/static/app.js",
  "/static/icon.svg",
  "/static/vendor/leaflet.js",
  "/static/vendor/leaflet.css",
  "/manifest.webmanifest",
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;
  // Never serve API calls from cache.
  if (url.pathname.startsWith("/api/")) return;
  if (url.origin !== location.origin) return;

  event.respondWith(
    caches.match(event.request).then(hit => {
      const network = fetch(event.request).then(res => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(event.request, copy));
        }
        return res;
      }).catch(() => hit);
      return hit || network;
    })
  );
});
