/// <reference types="vite/client" />

// Only `VITE_`-prefixed variables are exposed to client code -- Vite strips
// everything else, so an unprefixed name reads as `undefined` however the
// .env file spells it. Declaring them here does not enforce that (vite/client
// merges in an index signature, which is why a bare `DEMO_JOB` type-checked
// happily and silently disabled the demo), but it does say what exists.
interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  /** `1` in `--mode demo` only: installs the static-snapshot fetch shim. */
  readonly VITE_DEMO_JOB?: string;
  /** Apps Script endpoint for the demo waitlist form. */
  readonly VITE_WAITLIST_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
