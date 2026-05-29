const CACHE_NAME = 'jarvis-hud-v1';
const ASSETS_TO_CACHE = [
  '/',
  '/manifest.json',
  '/assets/icon_1024.png'
];

// Install Event — pre-cache the static shell assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE);
    }).then(() => self.skipWaiting())
  );
});

// Activate Event — clean old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cache) => {
          if (cache !== CACHE_NAME) {
            return caches.delete(cache);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch Event — Network-first for API requests, Cache-first for offline HUD assets
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Strictly skip caching for streaming routes, SSE, or remote control API requests
  if (
    url.pathname.startsWith('/chat') ||
    url.pathname.startsWith('/status') ||
    url.pathname.startsWith('/bridge') ||
    url.pathname.startsWith('/remote')
  ) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Cache-first strategy for main static shell
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        // Asynchronously fetch fresh in the background and update cache
        fetch(event.request).then((networkResponse) => {
          if (networkResponse.status === 200) {
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, networkResponse));
          }
        }).catch(() => {}); // ignore network failures in background update

        return cachedResponse;
      }
      return fetch(event.request);
    })
  );
});
