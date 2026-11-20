// Service worker панели оператора: нужен, чтобы панель ставилась как приложение,
// и открывает последнюю загруженную версию без сети. API не кешируется.
const CACHE = "signalkg-panel-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k.startsWith("signalkg-panel-") && k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || url.pathname !== "/") return;
  event.respondWith(
    fetch(event.request)
      .then(response => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put("/", copy));
        }
        return response;
      })
      .catch(() => caches.match("/"))
  );
});
