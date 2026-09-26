/**
 * The Job Radar mark: a contact, the ring it sits in, and the sweep still
 * going round.
 *
 * The same geometry is in `public/favicon.svg`, which is where the tab icon,
 * the ICO fallback and the iOS icon come from — edit both together, and re-run
 * `scripts/make_icons.py` after. It is duplicated rather than imported because
 * the favicon carries its own violet tile (browser chrome gives it no
 * background), while in the app the tile is a styled element around this glyph:
 * the rail animates its rim, the sync screen pulses it, and the sync screen
 * swaps the gradient to red on failure. Importing the file would nest a second
 * tile inside those.
 *
 * It fills whatever box it is given, so the glyph keeps the favicon's
 * proportions — two thirds of the tile — at every size it is used at.
 */
export function BrandGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 48 48" aria-hidden className={className} fill="none">
      <circle cx="24" cy="24" r="4" fill="currentColor" />
      <circle cx="24" cy="24" r="9.2" stroke="currentColor" strokeWidth="3" />
      <path
        d="M35.19 33.39A14.6 14.6 0 0 0 14.61 12.82"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  );
}
