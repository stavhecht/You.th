import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    // Proxying keeps the app same-origin in dev, so the API contract doesn't
    // change shape between dev and production. The target is configurable for
    // one reason: inside docker compose the API is `backend`, not localhost.
    proxy: { "/api": process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000" },
    // Bind mounts on Docker Desktop do not deliver inotify events reliably, so
    // the container sets this and HMR keeps working. Off by default - polling
    // burns CPU for nothing when the files are local.
    watch: process.env.VITE_DEV_POLL ? { usePolling: true, interval: 300 } : undefined,
  },
});
