/*
 * Kelly Family Office — Service Worker
 *
 * Makes the app usable offline. Strategy:
 *   - Precache the app shell (vendored libraries, icons, CSS, offline page) on install.
 *   - Navigations (HTML pages): network-first, falling back to the last cached copy of
 *     that page, then to a generic offline page. This keeps data fresh when online while
 *     letting the family review the last-loaded view with no connection.
 *   - Static assets (vendor JS, icons, CSS): stale-while-revalidate for instant loads.
 *   - LLM / agent / mutating requests: network-only (never cached, never served stale).
 *
 * Served from "/service-worker.js" so its scope covers the whole origin.
 */

const VERSION = "kfo-v1";
const STATIC_CACHE = `${VERSION}-static`;
const PAGE_CACHE = `${VERSION}-pages`;

// App shell — always available offline once installed.
const PRECACHE_URLS = [
  "/offline",
  "/static/vendor/tailwind.min.js",
  "/static/vendor/htmx.min.js",
  "/static/vendor/chart.umd.min.js",
  "/static/css/app.css",
  "/static/js/charts.js",
  "/static/icons/app-icon.svg",
  "/manifest.webmanifest",
];

// Never serve a cached response for these — always go to the network.
const NETWORK_ONLY = ["/agent/", "/health"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(STATIC_CACHE)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => !k.startsWith(VERSION))
            .map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

function isStaticAsset(url) {
  return (
    url.pathname.startsWith("/static/") ||
    url.pathname === "/manifest.webmanifest"
  );
}

function isNetworkOnly(url) {
  return NETWORK_ONLY.some((p) => url.pathname.startsWith(p));
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle same-origin GET. Let everything else (POST, cross-origin) pass through.
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (isNetworkOnly(url)) return;

  // Navigations (page loads): network-first with offline fallback.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(PAGE_CACHE).then((c) => c.put(request, copy));
          return resp;
        })
        .catch(() =>
          caches
            .match(request)
            .then((cached) => cached || caches.match("/offline"))
        )
    );
    return;
  }

  // Static assets: stale-while-revalidate.
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.open(STATIC_CACHE).then((cache) =>
        cache.match(request).then((cached) => {
          const network = fetch(request)
            .then((resp) => {
              if (resp && resp.status === 200) cache.put(request, resp.clone());
              return resp;
            })
            .catch(() => cached);
          return cached || network;
        })
      )
    );
    return;
  }

  // Other same-origin GETs (fragments, JSON): stale-while-revalidate in the page cache.
  event.respondWith(
    caches.open(PAGE_CACHE).then((cache) =>
      cache.match(request).then((cached) => {
        const network = fetch(request)
          .then((resp) => {
            if (resp && resp.status === 200) cache.put(request, resp.clone());
            return resp;
          })
          .catch(() => cached);
        return cached || network;
      })
    )
  );
});

// Allow the page to trigger an immediate update.
self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});
