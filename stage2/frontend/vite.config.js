import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api and /ws to the backend so the frontend runs on
// :5173 with hot reload while talking to the real service on :8080 - same
// origin from the browser's point of view, so no CORS or cookie surprises
// between `npm run dev` and the built bundle the backend serves itself.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8080", changeOrigin: true },
      "/ws": { target: "ws://localhost:8080", ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        // three.js is ~600 kB and never changes between deploys; splitting it
        // out keeps it cached across releases of the app code.
        manualChunks: { three: ["three"] },
      },
    },
  },
});
