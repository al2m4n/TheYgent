import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// SPA only (M8 §6: no SSR). Vite serves the cockpit on :5173; the control-plane CORS
// allows that origin. The SPA is never bundled into the FastAPI app.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
  },
  test: {
    globals: true,
    environment: "jsdom",
    // Playwright lives under tests/e2e and is run by its own runner, not Vitest.
    exclude: ["**/node_modules/**", "**/tests/e2e/**"],
  },
});
