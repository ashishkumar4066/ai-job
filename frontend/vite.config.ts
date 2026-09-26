import { defineConfig, type Connect, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

/**
 * The local stand-in for the PDF rewrite in `vercel.json`.
 *
 * The tailored-résumé preview is an `<iframe src="/api/documents/3/pdf?v=…">`,
 * which never passes through `window.fetch`, so the demo's shim cannot answer
 * it. Production solves that with a Vercel rewrite; this makes `dev:demo` and
 * `preview:demo` behave the same way, so the demo can be checked before deploy
 * instead of after.
 *
 * Only added in `--mode demo`, so it is absent from every normal run.
 */
function demoPdfRewrite(): Plugin {
  const middleware: Connect.NextHandleFunction = (req, _res, next) => {
    const match = req.url?.match(/^\/api\/documents\/(\d+)\/pdf(?:\?|$)/);
    if (match) req.url = `/demo/pdf/${match[1]}.pdf`;
    next();
  };
  return {
    name: "demo-pdf-rewrite",
    apply: (_config, env) => env.mode === "demo",
    configureServer(server) {
      server.middlewares.use(middleware);
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware);
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), demoPdfRewrite()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    // Talk to the Phase 1 API without CORS juggling in dev.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
