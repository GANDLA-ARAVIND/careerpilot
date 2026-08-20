import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      // import.meta.dirname, not __dirname - Vite warns that __dirname is
      // unsupported by the native config loader that becomes the default
      // in a future major.
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    // Proxy /api to the FastAPI backend so the browser only ever talks to
    // one origin in dev. The backend also sets CORS for :5173 (see
    // api/main.py), so direct cross-origin calls would work too - but
    // proxying means the frontend needs no base-URL configuration at all,
    // and the same relative paths keep working if this is ever served as
    // static files from the backend itself.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        // SSE (/api/run/stream) must not be transformed or cached by the
        // proxy, or the Mission Control timeline would arrive in one lump
        // at the end instead of streaming.
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            if (proxyRes.headers["content-type"]?.includes("text/event-stream")) {
              proxyRes.headers["cache-control"] = "no-cache, no-transform"
            }
          })
        },
      },
    },
  },
})
