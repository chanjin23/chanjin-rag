import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 개발 중에는 /api 요청을 로컬 FastAPI(기본 127.0.0.1:8000)로 넘긴다.
// E2E는 API_TARGET으로 별도 포트의 백엔드를 가리킨다. 운영에서는 Nginx가 같은 역할을 맡는다 (TRD 3.2).
const apiTarget = process.env.API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
    },
  },
});
