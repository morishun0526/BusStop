// オフライン対応: 画面はキャッシュ優先、時刻表データはネット優先（圏外時はキャッシュ）
const V = "bus-v2";
const SHELL = ["./", "index.html", "style.css", "app.js", "config.js", "manifest.webmanifest", "icons/icon-192.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(V).then(c => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return; // リアルタイム情報はキャッシュしない
  e.respondWith((async () => {
    const c = await caches.open(V);
    try {
      const r = await fetch(e.request);
      if (r.ok) c.put(e.request, r.clone());
      return r;
    } catch {
      return (await c.match(e.request)) || (await c.match("index.html"));
    }
  })());
});
