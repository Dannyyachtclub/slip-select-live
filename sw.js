// Slip Select — minimal network-passthrough service worker.
// No caching: every request goes straight to the network so status.json
// and the page are always fresh.
self.addEventListener('fetch', function (event) {
  event.respondWith(fetch(event.request));
});
