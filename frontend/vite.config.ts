import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import tailwindcss from "@tailwindcss/vite";

// SSE proxy helper: disable buffering so events stream immediately
const sseProxy = {
  target: "http://backend:8000",
  configure: (proxy: any) => {
    proxy.on("proxyRes", (proxyRes: any) => {
      if (proxyRes.headers["content-type"]?.includes("text/event-stream")) {
        proxyRes.headers["cache-control"] = "no-cache";
        proxyRes.headers["connection"] = "keep-alive";
        proxyRes.headers["x-accel-buffering"] = "no";
      }
    });
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    proxy: {
      "/health": { target: "http://backend:8000" },
      "/sessions": { target: "http://backend:8000" },
      "/agents": sseProxy,
      "/teams": sseProxy,
      "/settings": { target: "http://backend:8000" },
      "/upload": { target: "http://backend:8000" },
    },
  },
});
