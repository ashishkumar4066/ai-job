/**
 * The demo's own UI: a waitlist button and the form behind it.
 *
 * Mounted into a `<div>` appended to `<body>` with its own `createRoot`, which is
 * why no existing component is edited to make room for it. `App.tsx`, `TopBar`
 * and the nav rail are untouched, and deleting this folder removes the overlay
 * with them none the wiser.
 *
 * Styling reads the app's own CSS custom properties (`--panel-strong`,
 * `--accent`, `--text-muted`, set in `index.css` and re-declared under `.dark`),
 * so it follows the theme toggle without knowing the toggle exists. The rules are
 * in one injected stylesheet rather than Tailwind classes, so the overlay stays
 * independent of the design system's utility config.
 */

import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import * as state from "./state";
import { joinWaitlist, looksLikeEmail } from "./waitlist";

const CSS = `
/* Bottom padding clears the app's own keyboard-hint footer, which the bar
   sat directly on top of at 14px. Reduced under 560px, where that footer
   is not rendered. */
.demo-layer { position: fixed; inset: auto 0 0 0; z-index: 2147483000; pointer-events: none;
  display: flex; justify-content: center; padding: 0 12px 46px; font: inherit; }
.demo-layer > * { pointer-events: auto; }

/* Opaque, not translucent.
   The first version used a backdrop-filter over --panel-strong, which is a
   70-88% alpha surface. Over the jobs table that let row text bleed straight
   through the bar and collide with its own copy — the notice was sitting on
   the content rather than above it. A solid surface with a real border and a
   deep shadow is the fix; a blur cannot buy contrast that the alpha gives
   away. */
.demo-bar { display: flex; align-items: center; gap: 6px;
  padding: 7px 8px 7px 9px; border-radius: 999px;
  background: var(--bg-elevated, #fff); color: var(--text, #111);
  border: 1px solid color-mix(in oklab, var(--accent, #6366f1) 42%, transparent);
  box-shadow:
    0 18px 44px rgba(0, 0, 0, .5),
    0 2px 10px rgba(0, 0, 0, .3),
    0 0 0 4px color-mix(in oklab, var(--accent, #6366f1) 12%, transparent); }

.demo-btn { flex: none; border: 0; cursor: pointer; font: inherit; font-size: 13.5px; font-weight: 700;
  padding: 9px 18px; border-radius: 999px; background: var(--accent, #6366f1); color: #fff;
  letter-spacing: .005em; white-space: nowrap;
  box-shadow: 0 3px 14px var(--accent-glow, rgba(99,102,241,.45)); }
.demo-btn:hover { filter: brightness(1.08); }
.demo-btn:disabled { opacity: .6; cursor: default; }
.demo-ghost { background: transparent; color: var(--text-muted, #666); box-shadow: none;
  padding: 7px 8px; font-weight: 500; }
.demo-ghost:hover { color: var(--text, #111); filter: none; }

.demo-scrim { position: fixed; inset: 0; z-index: 2147483100; display: grid; place-items: center;
  padding: 16px; background: rgba(10, 6, 16, .55); backdrop-filter: blur(3px); }
.demo-modal { width: min(440px, 100%); border-radius: 18px; padding: 22px;
  background: var(--popover, #fff); color: var(--text, #111);
  border: 1px solid var(--border-strong, rgba(0,0,0,.12));
  box-shadow: 0 24px 64px rgba(0,0,0,.35); }
.demo-modal h2 { margin: 0 0 6px; font-size: 18px; font-weight: 680; letter-spacing: -.01em; }
.demo-modal p { margin: 0 0 16px; font-size: 13.5px; line-height: 1.5; color: var(--text-muted, #666); }
.demo-modal label { display: block; font-size: 12px; font-weight: 600; margin: 0 0 5px; }
.demo-modal input, .demo-modal textarea { width: 100%; box-sizing: border-box; font: inherit; font-size: 14px;
  padding: 9px 11px; border-radius: 10px; color: var(--text, #111);
  border: 1px solid var(--border-strong, rgba(0,0,0,.15)); background: var(--bg-elevated, #fff); }
.demo-modal input:focus, .demo-modal textarea:focus { outline: 2px solid var(--accent, #6366f1); outline-offset: 1px; }
.demo-modal textarea { min-height: 62px; resize: vertical; }
.demo-field { margin-bottom: 12px; }
.demo-row { display: flex; gap: 8px; justify-content: flex-end; align-items: center; margin-top: 4px; }
.demo-note { font-size: 12.5px; margin: 0 0 12px; }
.demo-note.bad { color: var(--danger, #dc2626); }
.demo-note.good { color: var(--mint, #059669); }
.demo-modal a { color: var(--accent-text, #4f46e5); }

@media (max-width: 560px) {
  .demo-layer { padding-bottom: 14px; }
}
@media (prefers-reduced-motion: no-preference) {
  .demo-layer { animation: demo-rise .32s cubic-bezier(.2,.8,.2,1) both; }
  @keyframes demo-rise { from { transform: translateY(14px); opacity: 0 } to { transform: none; opacity: 1 } }
}
`;

function WaitlistModal({ onClose }: { onClose: () => void }) {
  const [email, setEmail] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

  // Escape closes, and focus starts in the field — the two things a dialog owes
  // a keyboard user. Not a full focus trap: this overlay has one input and two
  // buttons, and a trap that misbehaves is worse than none.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    const outcome = await joinWaitlist(email, note);
    setResult(outcome);
    setBusy(false);
    if (outcome.ok) state.markWaitlisted();
  }

  return (
    <div className="demo-scrim" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="demo-modal" role="dialog" aria-modal="true" aria-labelledby="demo-waitlist-title">
        <h2 id="demo-waitlist-title">Join the waitlist</h2>
        {result?.ok ? (
          <>
            <p className="demo-note good">{result.message}</p>
            <div className="demo-row">
              <button type="button" className="demo-btn" onClick={onClose}>
                Close
              </button>
            </div>
          </>
        ) : (
          <form onSubmit={submit}>
            <p>
              This runs privately against one person's job search today. Leave an address and I'll
              get in touch when there's a version other people can point at their own board.
            </p>
            {result && !result.ok && <p className="demo-note bad">{result.message}</p>}
            <div className="demo-field">
              <label htmlFor="demo-email">Email</label>
              <input
                id="demo-email"
                type="email"
                autoFocus
                required
                value={email}
                placeholder="you@example.com"
                onChange={(event) => setEmail(event.target.value)}
              />
            </div>
            <div className="demo-field">
              <label htmlFor="demo-note">What would you want it to do? (optional)</label>
              <textarea
                id="demo-note"
                value={note}
                placeholder="The boards you care about, the part of this you'd actually use…"
                onChange={(event) => setNote(event.target.value)}
              />
            </div>
            <div className="demo-row">
              <button type="button" className="demo-btn demo-ghost" onClick={onClose}>
                Not now
              </button>
              <button type="submit" className="demo-btn" disabled={busy || !looksLikeEmail(email)}>
                {busy ? "Sending…" : "Join waitlist"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

/**
 * The call to action, and nothing else.
 *
 * This used to carry a line explaining that the page was a snapshot and that
 * nothing costing an API call would run. That copy is gone by request: it
 * explained the build to someone who has not asked how it was built, and it
 * competed with the one thing the overlay exists to do. The demo says what it
 * is through the product; the overlay just asks for an address.
 *
 * Nothing is awaited before this renders. The earlier version held the bar back
 * until the snapshot had loaded, because it quoted a posting count — with the
 * count gone, so is the reason to wait, and the button is there from the first
 * paint.
 */
function Overlay() {
  const [dismissed, setDismissed] = useState(false);
  const [modal, setModal] = useState(false);

  return (
    <>
      {/* Dismissal is component state, not persisted: hiding it is a "not
          now", and a reload should offer again. Nothing here is worth
          remembering across visits except having actually signed up. */}
      {!dismissed && (
        <div className="demo-layer">
          <div className="demo-bar">
            <button type="button" className="demo-btn" onClick={() => setModal(true)}>
              {state.isWaitlisted() ? "On the list ✓" : "Join waitlist"}
            </button>
            <button
              type="button"
              className="demo-btn demo-ghost"
              aria-label="Hide the waitlist button"
              onClick={() => setDismissed(true)}
            >
              ✕
            </button>
          </div>
        </div>
      )}
      {modal && <WaitlistModal onClose={() => setModal(false)} />}
    </>
  );
}

let mounted = false;

export function mountOverlay(): void {
  if (mounted) return;
  mounted = true;

  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.append(style);

  const host = document.createElement("div");
  host.id = "demo-overlay-root";
  document.body.append(host);
  createRoot(host).render(<Overlay />);
}
