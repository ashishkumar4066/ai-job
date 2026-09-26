/**
 * The demo's own UI: a snapshot notice and the waitlist form.
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
import { loadSnapshot, type Manifest } from "./snapshot";
import * as state from "./state";
import { joinWaitlist, looksLikeEmail } from "./waitlist";

const REPO = "https://github.com/ashishkumar4066/ai-job";

const CSS = `
/* Bottom padding clears the app's own keyboard-hint footer, which the bar
   sat directly on top of at 14px. Reduced under 560px, where that footer
   is not rendered. */
.demo-layer { position: fixed; inset: auto 0 0 0; z-index: 2147483000; pointer-events: none;
  display: flex; justify-content: center; padding: 0 12px 46px; font: inherit; }
.demo-layer > * { pointer-events: auto; }

.demo-bar { display: flex; align-items: center; gap: 12px; max-width: min(720px, 100%);
  padding: 9px 10px 9px 14px; border-radius: 999px;
  background: var(--panel-strong, rgba(255,255,255,.9)); color: var(--text, #111);
  border: 1px solid var(--border-strong, rgba(0,0,0,.12));
  box-shadow: var(--shadow-float, 0 12px 32px rgba(0,0,0,.18));
  backdrop-filter: blur(14px) saturate(160%); -webkit-backdrop-filter: blur(14px) saturate(160%); }
.demo-bar p { margin: 0; font-size: 13px; line-height: 1.35; }
.demo-bar b { font-weight: 650; }
.demo-bar small { display: block; color: var(--text-muted, #666); font-size: 11.5px; }

.demo-dot { flex: none; width: 8px; height: 8px; border-radius: 50%;
  background: var(--mint, #34d399); box-shadow: 0 0 0 3px color-mix(in oklab, var(--mint, #34d399) 25%, transparent); }

.demo-btn { flex: none; border: 0; cursor: pointer; font: inherit; font-size: 13px; font-weight: 600;
  padding: 7px 14px; border-radius: 999px; background: var(--accent, #6366f1); color: #fff;
  box-shadow: 0 2px 10px var(--accent-glow, rgba(99,102,241,.4)); }
.demo-btn:hover { filter: brightness(1.08); }
.demo-btn:disabled { opacity: .6; cursor: default; }
.demo-ghost { background: transparent; color: var(--text-muted, #666); box-shadow: none;
  padding: 7px 8px; font-weight: 500; }
.demo-ghost:hover { color: var(--text, #111); filter: none; }

.demo-pill { border: 1px solid var(--border, rgba(0,0,0,.1)); background: var(--panel, rgba(255,255,255,.7));
  color: var(--text-muted, #555); border-radius: 999px; padding: 6px 13px; font-size: 12px; font-weight: 600;
  cursor: pointer; backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); }
.demo-pill:hover { color: var(--text, #111); }

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
.demo-stats { display: flex; flex-wrap: wrap; gap: 6px 14px; margin: 0 0 16px; padding: 0; list-style: none; }
.demo-stats li { font-size: 12px; color: var(--text-muted, #666); }
.demo-stats b { display: block; font-size: 17px; font-weight: 680; color: var(--text, #111); letter-spacing: -.02em; }
.demo-modal a { color: var(--accent-text, #4f46e5); }

@media (max-width: 560px) {
  .demo-layer { padding-bottom: 14px; }
  .demo-bar { flex-wrap: wrap; border-radius: 16px; }
  .demo-bar p { flex: 1 1 100%; }
}
@media (prefers-reduced-motion: no-preference) {
  .demo-layer { animation: demo-rise .32s cubic-bezier(.2,.8,.2,1) both; }
  @keyframes demo-rise { from { transform: translateY(14px); opacity: 0 } to { transform: none; opacity: 1 } }
}
`;

const nf = new Intl.NumberFormat("en-US");

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

function Overlay() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [modal, setModal] = useState(false);

  useEffect(() => {
    let alive = true;
    void loadSnapshot().then((snapshot) => alive && setManifest(snapshot.manifest));
    return () => {
      alive = false;
    };
  }, []);

  // Held back until the snapshot is in: a bar that says "0 jobs" for the first
  // second is worse than a bar that arrives a second late.
  if (!manifest) return null;

  const taken = new Date(manifest.generated_at);

  return (
    <>
      <div className="demo-layer">
        {collapsed ? (
          <button type="button" className="demo-pill" onClick={() => setCollapsed(false)}>
            Demo · join waitlist
          </button>
        ) : (
          <div className="demo-bar">
            <span className="demo-dot" aria-hidden />
            <p>
              <b>Live demo.</b> A real snapshot of {nf.format(manifest.counts.jobs)} postings, taken{" "}
              {taken.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}.
              <small>
                Everything is clickable; nothing that costs an API call runs.{" "}
                <a href={REPO} target="_blank" rel="noreferrer">
                  Source
                </a>
              </small>
            </p>
            <button type="button" className="demo-btn" onClick={() => setModal(true)}>
              {state.isWaitlisted() ? "On the list ✓" : "Join waitlist"}
            </button>
            <button
              type="button"
              className="demo-btn demo-ghost"
              aria-label="Collapse the demo notice"
              onClick={() => setCollapsed(true)}
            >
              ✕
            </button>
          </div>
        )}
      </div>
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
