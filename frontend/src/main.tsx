import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import './index.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      //
      retry: (failureCount, error) => {
        // A dead backend won't heal by retrying — surface it immediately.
        if (error instanceof Error && 'status' in error && error.status === 0)
          return false;
        return failureCount < 2;
      },
    },
  },
});

function mount() {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </StrictMode>,
  );
}

// The public demo build (`VITE_DEMO_JOB=1`) answers every API call from a static
// snapshot instead of from the backend. Dynamically imported, so a normal build
// never loads `src/demo/` at all; installed before `mount()`, so no query can
// escape to a backend that is not there. A `.then` rather than top-level await,
// which the build target does not allow. Unset — every local run — this is one
// comparison and a direct mount.
if (import.meta.env.VITE_DEMO_JOB === '1') {
  void import('./demo/install').then(({ installDemo }) => {
    installDemo();
    mount();
  });
} else {
  mount();
}
