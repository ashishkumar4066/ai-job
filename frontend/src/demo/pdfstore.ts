/**
 * The page's half of the generated-PDF handoff.
 *
 * `public/demo-sw.js` answers `/api/documents/{id}/pdf` from a Cache Storage
 * entry; this puts the entries there and reports whether that worker is
 * actually in control, because the demo must not tell a visitor a document
 * compiled if nothing can serve the result.
 */

const CACHE = "demo-generated-pdf-v1";

let ready: Promise<boolean> | null = null;

/** Register the worker. Idempotent, and safe to call before anything needs it. */
export function installPdfWorker(): Promise<boolean> {
  ready ??= (async () => {
    if (!("serviceWorker" in navigator) || !("caches" in window)) return false;
    try {
      const base = import.meta.env.BASE_URL ?? "/";
      await navigator.serviceWorker.register(`${base}demo-sw.js`, { scope: base });
      // `ready` resolves once a worker controls this page, which is what the
      // iframe actually depends on — a registration that has not activated yet
      // would let the first preview 404.
      await navigator.serviceWorker.ready;
      return true;
    } catch {
      // A blocked worker (file://, no HTTPS, a locked-down profile) is not an
      // error worth surfacing; the caller falls back to saying so.
      return false;
    }
  })();
  return ready;
}

/**
 * Forget a generated PDF, so the path falls back to the network.
 *
 * Reverting a document that ships with the snapshot has to reach the exported
 * Tectonic file again; leaving a rendered one in the cache would keep serving
 * the edited version after the edit was undone.
 */
export async function dropPdf(id: number): Promise<void> {
  if (!("caches" in window)) return;
  try {
    const cache = await caches.open(CACHE);
    await cache.delete(`${window.location.origin}/__demo-pdf/${id}`);
  } catch {
    /* Nothing cached is the state we wanted anyway. */
  }
}

/** Make `bytes` the answer for `/documents/{id}/pdf`. */
export async function storePdf(id: number, bytes: Uint8Array): Promise<boolean> {
  if (!(await installPdfWorker())) return false;
  try {
    const cache = await caches.open(CACHE);
    await cache.put(
      `${window.location.origin}/__demo-pdf/${id}`,
      new Response(bytes.slice().buffer as ArrayBuffer, {
        headers: {
          "Content-Type": "application/pdf",
          "Cache-Control": "no-store",
        },
      }),
    );
    return true;
  } catch {
    return false;
  }
}
