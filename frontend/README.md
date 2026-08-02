# Phase 2A — Dashboard

A React dashboard over the Phase 1 API: browse, filter and inspect every
aggregated posting. Validation (badges, scores) is Phase 2B.

## Run it

The backend must be running first:

```bash
# terminal 1
cd backend && uvicorn app.main:app --reload      # :8000

# terminal 2
cd frontend && npm install && npm run dev        # :5173
```

Vite proxies `/api/*` → `http://127.0.0.1:8000`, so there is no CORS setup in
dev. For a deployed backend, set `VITE_API_BASE`.

```bash
npm run build       # tsc -b && vite build  ->  dist/
npm run typecheck
```

## Stack

React 19 · Vite 6 · TypeScript (strict) · Tailwind v4 · TanStack Query ·
TanStack Virtual · lucide-react. No router, no date library, no component
library — URL state is handled with `URLSearchParams` and dates with `Intl`.

## What's here

**Virtualized list.** `@tanstack/react-virtual` keeps ~18 rows in the DOM no
matter how large the result set is, with infinite paging (100 rows/request) as
you scroll. Verified against 3,391 live jobs.

**URL as the single source of truth.** Every filter, the sort, and the open job
live in the query string, so any view is bookmarkable and the back button does
what you expect. Only non-default values are serialized (`parseFilters` /
`serializeFilters` in [src/lib/useFilters.ts](src/lib/useFilters.ts)).

**Facet-driven filters.** Dropdown options and their counts come from
`GET /meta/facets`, which shares its filter builder with `GET /jobs` on the
server — so the counts can never drift from the results.

**New since last visit.** A `localStorage` timestamp, read once and frozen for
the session so rows don't stop being "new" while you're reading them. Drives
the mint dot on rows, the header pill, and the "New only" filter.

**Keyboard.** `/` search · `j`/`k` move between jobs · `r` re-ingest · `t`
theme · `esc` close.

## Layout

```
src/
  App.tsx                 queries, state wiring, hotkeys
  lib/
    api.ts                fetch client + filters -> query params
    types.ts              mirrors the Phase 1 Pydantic schemas
    useFilters.ts         URL <-> filter state (the round-trip contract)
    hooks.ts              theme, last-visit, debounce, dismiss, hotkeys
    format.ts             relative dates, locations, company chips
  components/
    TopBar / StatsStrip / FilterBar / MultiSelect
    JobTable              virtualized rows
    JobDrawer             detail + full JD + apply
    primitives.tsx        Badge, Segmented, skeletons, empty states
  index.css               design tokens, glass utilities, JD typography
```

## Design notes

**One palette.** A single brand hue (indigo, `--brand-hue: 264`) carries every
interactive state, and the neutrals are that same hue held at very low chroma
so the greys read as family rather than as a second colour. Exactly two colours
break the monochrome, each with one job:

| Token | Used for | Never used for |
| --- | --- | --- |
| `--accent` | selection, active filters, links, remote badges | alerts |
| `--highlight` (amber) | "new since last visit" | anything else |
| `--danger` (red) | closed postings, errors | emphasis |

Company chips are generated from a hash but constrained to a ±35° band around
the brand hue (`companyHue` in `lib/format.ts`), so the list stays
distinguishable without becoming a rainbow. **To re-skin the app, change
`--brand-hue` and the accent lines in `index.css`.**

Tokens are semantic CSS variables (`--panel`, `--ink`, `--accent`…) that flip
per theme and are exposed to Tailwind with `@theme inline`, so `bg-panel` is
correct in both themes without any `dark:` variants. Colours are `oklch` for
even perceptual steps.

Surfaces: `.glass` / `.glass-strong` (blur + saturate + hairline border),
`.glass-popover` for floating menus, and `.glass-sheen` for the light catch
along a top edge. An animated aurora sits behind everything at `z-index: -1`.
All motion collapses under `prefers-reduced-motion`.

> **`.glass-sheen` deliberately sets no `position`.** Tailwind v4 emits custom
> utilities *after* its core ones, so a `position: relative` here silently beat
> the `absolute` on any popover combining the two classes — the dropdown fell
> back into normal flow and pushed the filter bar open. Callers own their
> positioning.

Popovers use `--popover` (~97% opaque) rather than a panel token: backdrop blur
alone does not stop high-contrast rows bleeding through a floating menu.

Job descriptions are ATS-authored HTML with wildly inconsistent markup, so
`.jd` in [src/index.css](src/index.css) re-imposes typography rather than
trusting the source.

## Known gaps (deliberate, land in 2B)

- Job descriptions render via `dangerouslySetInnerHTML`. The HTML comes from
  the same-origin Phase 1 API for a personal-scale tool, but it should be
  sanitized when the validation layer lands.
- No validity badge or `min validity` filter yet — that is 2B's scope.
