import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// SPA only (same posture as the cockpit — no SSR, Vite-served, never bundled into FastAPI).
// The interface runs on :5174 so it can sit alongside the cockpit (:5173) during dev. Both call
// the control-plane directly; the control-plane CORS allows both dev origins.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // `@/` → `src/` — the import convention the component generator writes against
    // (mirrored in tsconfig `paths` so tsc resolves the same specifiers).
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // Serve `static/` at the site root (brand artwork, favicons) — referenced by literal path
  // (e.g. `/logo/…`) rather than imported, so the same URL resolves in dev and in the build.
  publicDir: "static",
  server: {
    // Defaults to :5174 (alongside the cockpit on :5173). A PORT override lets a second instance
    // run on a free port (e.g. a preview server beside an already-running `pnpm dev`).
    port: Number(process.env.PORT) || 5174,
    strictPort: !process.env.PORT,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    exclude: ["**/node_modules/**", "**/dist/**"],
  },
});
