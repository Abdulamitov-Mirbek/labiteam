// Service worker приложения для жителей: оболочка открывается и без сети.
// Запросы к API не кешируются — приложение само показывает последний результат
// и держит жалобы в очереди, пока сети нет.
const CACHE = "signalkg-app-v3";
const SHELL = ["/app/", "/app/manifest.webmanifest", "/app/icon.svg", "/app/icon-192.png", "/app/icon-512.png",
  "/app/leaflet.js", "/app/leaflet.css"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k.startsWith("signalkg-app-") && k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // «Поделиться» открывает /app/?url=… — в кеше это та же страница /app/, а не новая запись
  // на каждую ссылку. Без сети страница открывается из кеша и ставит объявление в очередь.
  const key = url.pathname === "/app/" ? "/app/" : event.request;
  // Сначала сеть (чтобы получать обновления), без сети — кеш.
  event.respondWith(
    fetch(event.request)
      .then(response => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put(key, copy));
        }
        return response;
      })
      .catch(() => caches.match(key).then(hit => hit || caches.match("/app/")))
  );
});
