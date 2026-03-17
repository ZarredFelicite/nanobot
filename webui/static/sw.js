const CACHE_NAME = 'nanobot-v1';
const STATIC_ASSETS = ['/', '/manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Network-only for SSE and API calls — pass through without caching
  const apiPrefixes = ['/event', '/session', '/provider', '/config', '/command', '/agent', '/log'];
  if (apiPrefixes.some((p) => url.pathname.startsWith(p))) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Network-only for push subscription endpoints
  if (url.pathname.startsWith('/push')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Cache-first for static assets (GET only)
  if (event.request.method === 'GET') {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((response) => {
          if (response.ok && response.type === 'basic') {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        });
      })
    );
  }
});

// Web Push notification handler
self.addEventListener('push', (event) => {
  if (!event.data) return;

  let data;
  try {
    data = event.data.json();
  } catch {
    data = { title: 'Nanobot', body: event.data.text() };
  }

  event.waitUntil(
    self.registration.showNotification(data.title || 'Nanobot', {
      body: data.body || '',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      tag: 'nanobot-message',
      renotify: true,
      data: { url: data.url || '/' }
    })
  );
});

// Click notification to open/focus the app
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (new URL(client.url).pathname === '/' && 'focus' in client) {
          return client.focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
