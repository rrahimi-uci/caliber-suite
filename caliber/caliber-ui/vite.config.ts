import path from "node:path";
import react from "@vitejs/plugin-react-swc";
import { defineConfig } from "vite";

/**
 * Vite config for the CALIBER SPA.
 *
 * The dev proxy forwards `/ajax-api/*` to the local MLflow server
 * (defaults to http://localhost:5000) so the SPA can run with HMR
 * (`npm run dev`) while still talking to the real CALIBER backend.
 *
 * `base` is configurable via `CALIBER_UI_BASE` so the same build can be
 * served from a reverse-proxied subpath (e.g. `/caliber/` behind
 * `MLFLOW_STATIC_PREFIX=/mlflow`).
 */
export default defineConfig({
  base: process.env["CALIBER_UI_BASE"] ?? "/caliber/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/ajax-api": {
        target: process.env["CALIBER_API_TARGET"] ?? "http://localhost:5000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    target: "es2020",
  },
});
