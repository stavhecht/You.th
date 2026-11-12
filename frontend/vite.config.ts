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
    // `xfwd` adds X-Forwarded-For, so the API's rate limits count the browser
    // and not this proxy. Without it every visitor shared one address, and ten
    // wrong passwords from anyone locked everyone out.
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000",
        xfwd: true,
      },
    },
    // Bind mounts on Docker Desktop do not deliver inotify events reliably, so
    // the container sets this and HMR keeps working. Off by default - polling
    // burns CPU for nothing when the files are local.
    watch: process.env.VITE_DEV_POLL ? { usePolling: true, interval: 300 } : undefined,
  },
});
