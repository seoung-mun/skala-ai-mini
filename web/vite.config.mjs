import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import frontend from "./server.js";

export default defineConfig({
  envDir: false,
  plugins: [
    vue(),
    {
      name: "finagent-api",
      configureServer(server) {
        // Use the same API proxy and contract fixtures in development and production.
        const handleRequest = frontend.createServer().listeners("request")[0];
        server.middlewares.use((req, res, next) => {
          const path = new URL(req.url, "http://localhost").pathname;
          if (
            ["/health", "/chat", "/chat/stream", "/sessions/reset"].includes(
              path,
            ) ||
            path.startsWith("/preview/")
          ) {
            handleRequest(req, res);
            return;
          }
          const publicFile =
            [
              "/",
              "/index.html",
              "/@vite/client",
              "/@id/__x00__plugin-vue:export-helper",
              "/node_modules/vite/dist/client/env.mjs",
            ].includes(path) ||
            /^\/src\/[\w/.-]+\.(vue|js|mjs|css)$/.test(path) ||
            /^\/node_modules\/\.vite\/deps\/[\w.-]+$/.test(path);
          if (publicFile) next();
          else {
            res.writeHead(404);
            res.end("찾을 수 없는 경로입니다.");
          }
        });
      },
    },
  ],
  server: { host: "127.0.0.1", port: 8600, strictPort: true },
});
