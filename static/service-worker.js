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

const VERSION = "kfo-v4";
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
  "/static/js/table-sort.js",
  "/static/icons/app-icon.svg",
  "/manifest.webmanifest",
];

// Never serve a cached response for these — always go to the network.
// Update checks especially: a stale-while-revalidate cache once reported a
// long-uninstalled version as "current" on the Settings page.
const NETWORK_ONLY = ["/agent/", "/health", "/settings/check-updates", "/settings/apply-update", "/settings/upload-update"];

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

async function handleNavigation(request) {
  // Try the network with a few quick retries before giving up — the local
  // server is the source of truth and almost always up, so a transient failure
  // (server briefly busy on a heavy page) shouldn't strand the user offline.
  const delays = [0, 300, 700];
  for (let i = 0; i < delays.length; i++) {
    if (delays[i]) await new Promise((r) => setTimeout(r, delays[i]));
    try {
      const resp = await fetch(request);
      caches.open(PAGE_CACHE).then((c) => c.put(request, resp.clone()));
      return resp;
    } catch (e) {
      /* retry */
    }
  }
  // Genuinely unreachable — serve this page's last cached copy, else the shell.
  const cached = await caches.match(request);
  return cached || caches.match("/offline");
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Only handle same-origin GET. Let everything else (POST, cross-origin) pass through.
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (isNetworkOnly(url)) return;

  // Navigations (page loads): network-first, but RESILIENT. The server is local,
  // so a failed fetch is almost always a transient hiccup (the box was briefly
  // busy rendering a heavy page) — not a real outage. Retry a few times with a
  // short backoff before ever falling back to the cached copy, and only show the
  // offline page as a true last resort.
  if (request.mode === "navigate") {
    event.respondWith(handleNavigation(request));
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
