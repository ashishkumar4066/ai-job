/**
 * Serves the PDFs the demo generates in the browser.
 *
 * The Tailor modal previews a document with `<iframe src="/api/documents/3/pdf">`.
 * An iframe is a navigation, not a `fetch`, so the demo's fetch shim never sees
 * it — the ten documents that ship with the snapshot work because `vercel.json`
 * rewrites that path onto a static file, and a document the visitor generated
 * has no static file to rewrite to.
 *
 * A service worker is the one interception point that covers a navigation. This
 * one answers only that exact path, only from a cache the page fills, and falls
 * through to the network for everything else — including the ten real PDFs,
 * whose rewrite still does the work.
 */

const CACHE = 'demo-generated-pdf-v1';
const PATH = /\/documents\/(\d+)\/pdf$/;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('fetch', (event) => {
  let url;
  try {
    url = new URL(event.request.url);
  } catch {
    return;
  }
  if (url.origin !== self.location.origin) return;
  const match = PATH.exec(url.pathname);
  if (!match) return;

  event.respondWith(
    (async () => {
      try {
        const cache = await caches.open(CACHE);
        const hit = await cache.match(`${url.origin}/__demo-pdf/${match[1]}`);
        if (hit) return hit;
      } catch {
        /* No Cache Storage (private window, blocked storage) — fall through to
           the network, which is right for every document that has a file. */
      }
      return fetch(event.request);
    })(),
  );
});
