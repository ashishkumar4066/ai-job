/**
 * Installs the demo layer. The only thing `main.tsx` knows about this folder.
 *
 * Interception happens at `window.fetch` rather than inside `lib/api.ts`, and
 * that choice is the whole reason the demo costs the app nothing: `api.ts` is the
 * single place the dashboard makes requests, so wrapping the layer *below* it
 * means no component, hook, type or query key is edited or even aware of the
 * demo. An endpoint added to `api.ts` later keeps working here for free.
 *
 * Nothing in this module runs unless `DEMO_JOB=1`, and `main.tsx` imports it
 * dynamically, so a normal build never loads the snapshot or this code.
 */

import { handle } from './router';
import { realFetch } from './snapshot';
import { mountOverlay } from './overlay';

/** Matches the `BASE` in `lib/api.ts`. */
const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api';

let installed = false;

/**
 * Was a request one of ours?
 *
 * Compared as a resolved pathname rather than by string prefix, so it works for
 * a relative `/api/jobs`, an absolute `https://host/api/jobs`, a `Request`
 * object and a `URL` — all four of which reach `fetch` in this app.
 */
function apiPath(input: RequestInfo | URL): string | null {
  let href: string;
  if (typeof input === 'string') href = input;
  else if (input instanceof URL) href = input.href;
  else href = input.url;

  let url: URL;
  try {
    url = new URL(href, window.location.origin);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin) return null;
  if (!url.pathname.startsWith(API_BASE)) return null;
  return url.pathname.slice(API_BASE.length) || '/';
}

async function readBody(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<unknown> {
  const raw =
    init?.body ??
    (input instanceof Request ? await input.clone().text() : undefined);
  if (raw == null) return undefined;
  if (typeof raw !== 'string') return raw; // FormData (the résumé upload) — refused anyway.
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/**
 * Land a first-time visitor on the Dashboard rather than the jobs table.
 *
 * The app's own default is `jobs`, and that is right for its owner, who opens
 * it to find work. A visitor arriving from a launch page is asking a different
 * question — "what is this?" — and the Dashboard answers it in one screen:
 * applications, pipeline, board health, token spend. The jobs table answers it
 * with 1,803 rows and no context.
 *
 * Done by rewriting the URL before React mounts, rather than by changing the
 * app's default, so `useFilters` and every existing link keep their meaning and
 * no app file is edited. `replaceState` leaves no extra history entry, so Back
 * still leaves the site on the first press.
 *
 * Only for a bare arrival. A URL that already names a `view`, or that points at
 * a specific `job`, is someone's deliberate link and is left exactly as it is —
 * including links carrying only campaign parameters like `?ref=producthunt`.
 */
function landOnDashboard(): void {
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.has('view') || url.searchParams.has('job')) return;
    url.searchParams.set('view', 'dashboard');
    window.history.replaceState(null, '', url.toString());
  } catch {
    /* A URL we cannot parse is not worth failing the boot over. */
  }
}

export function installDemo(): void {
  if (installed) return;
  installed = true;

  landOnDashboard();

  window.fetch = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const path = apiPath(input);
    if (path === null) return realFetch(input as RequestInfo, init);

    const href =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const url = new URL(href, window.location.origin);
    const method = (
      init?.method ?? (input instanceof Request ? input.method : 'GET')
    ).toUpperCase();

    // The PDF preview is an <iframe src>, not a fetch, so it never reaches here
    // — a rewrite in `vercel.json` maps that path onto the exported file. This
    // branch only catches code that fetches the PDF directly.
    if (/^\/documents\/\d+\/pdf$/.test(path)) {
      const id = path.split('/')[2];
      return realFetch(`${import.meta.env.BASE_URL ?? '/'}demo/pdf/${id}.pdf`);
    }

    return handle(method, path, url.searchParams, await readBody(input, init));
  };

  mountOverlay();
  // eslint-disable-next-line no-console
  console.info(
    '%cDemo mode%c — every API call is answered from a static snapshot. Source: github.com/ashishkumar4066/ai-job',
    'background:#6366f1;color:#fff;padding:2px 6px;border-radius:4px;font-weight:600',
    'color:inherit',
  );
}
