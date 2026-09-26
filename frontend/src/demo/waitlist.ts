/**
 * Waitlist sign-ups, posted to a Google Apps Script web app.
 *
 * Why Apps Script and not a serverless function: a static demo needs somewhere
 * to put an email address, and this is the only option that costs nothing, needs
 * no account beyond the Google one that already exists, and has no free-tier
 * submission cap to blow through on launch day. The endpoint is a Sheet; the rows
 * are the waitlist. `DEMO.md` has the script to paste.
 *
 * Two constraints the request shape follows from:
 *
 *   - Apps Script web apps do not answer CORS preflight. A request with
 *     `Content-Type: application/json` triggers one and fails. Sending
 *     `text/plain;charset=utf-8` — a CORS-safelisted value — does not, and Apps
 *     Script reads the JSON out of `e.postData.contents` either way.
 *   - The response is therefore not readable in the normal case, so success is
 *     inferred from the request not throwing. That is the honest limit of a
 *     no-backend setup, and it means a *network* failure is reported while a
 *     server-side failure is not.
 */

const ENDPOINT = (import.meta.env.VITE_WAITLIST_URL as string | undefined)?.trim();

export interface WaitlistResult {
  ok: boolean;
  message: string;
}

/** Deliberately permissive: this rejects typos, not unusual-but-valid addresses. */
export function looksLikeEmail(value: string): boolean {
  const trimmed = value.trim();
  return trimmed.length >= 5 && trimmed.length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(trimmed);
}

export async function joinWaitlist(email: string, note?: string): Promise<WaitlistResult> {
  if (!looksLikeEmail(email)) {
    return { ok: false, message: "That does not look like an email address." };
  }
  if (!ENDPOINT) {
    // A missing endpoint is a deploy misconfiguration, not a visitor's problem,
    // so it says so plainly rather than pretending the sign-up worked.
    return {
      ok: false,
      message: "The waitlist is not wired up on this deployment yet.",
    };
  }

  try {
    await fetch(ENDPOINT, {
      method: "POST",
      // See the note above: this exact value is what avoids the preflight.
      headers: { "Content-Type": "text/plain;charset=utf-8" },
      body: JSON.stringify({
        email: email.trim(),
        note: note?.trim() || null,
        source: "product-hunt-demo",
        referrer: document.referrer || null,
        at: new Date().toISOString(),
      }),
    });
    return { ok: true, message: "You're on the list. I'll email you when it opens up." };
  } catch {
    return { ok: false, message: "Could not reach the waitlist. Mind trying again?" };
  }
}
