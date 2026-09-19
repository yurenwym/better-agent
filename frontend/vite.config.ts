import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` 起 Vite dev server：改前端即时热更新，不需要再手动 `npm run build`。
// /api 反向代理到后端，保证同源、CSRF 与 Host 校验照常通过；SSE 事件流同样走这条代理。
const BACKEND_ORIGIN = process.env.BETTER_AGENT_BACKEND_ORIGIN ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: Number(process.env.BETTER_AGENT_DEV_PORT ?? 5173),
    proxy: {
      "/api": {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
