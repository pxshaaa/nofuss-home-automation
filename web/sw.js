// NoFuss Home service worker: starts the app from a local copy, shows push notifications and opens the app
// when one is tapped.
const SHELL = "shell-v1";
self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(["/", "/static/icon.png?v=2"])).catch(() => {}));
});
self.addEventListener("activate", (e) => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim())));
// The page and icons come from the cache instantly and are refreshed in the background, so a new version
// shows on the next launch. Everything under /api always goes to the Mac mini.
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  const key = e.request.mode === "navigate" ? "/" : e.request;
  if (key !== "/" && !url.pathname.startsWith("/static/")) return;
  e.respondWith(caches.open(SHELL).then(async (c) => {
    const hit = await c.match(key);
    const net = fetch(e.request).then((r) => { if (r.ok) c.put(key, r.clone()); return r; });
    if (hit) { e.waitUntil(net.catch(() => {})); return hit; }
    return net;
  }));
});

self.addEventListener("push", (e) => {
  const d = e.data ? e.data.json() : {};
  e.waitUntil(self.registration.showNotification(d.title || "NoFuss Home", {
    body: d.body || "",
    icon: "/static/icon-512.png",
    tag: d.tag || undefined,
    data: { url: d.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = e.notification.data?.url || "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    for (const w of wins) if ("focus" in w) { w.navigate(url); return w.focus(); }
    return self.clients.openWindow(url);
  }));
});
