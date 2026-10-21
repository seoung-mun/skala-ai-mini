const http = require("node:http");
const { readFile } = require("node:fs/promises");
const { join } = require("node:path");

const files = new Map([
  ["/", ["dist/index.html", "text/html; charset=utf-8"]],
  ["/index.html", ["dist/index.html", "text/html; charset=utf-8"]],
  ...["easy", "hard", "error", "v1"].map((name) => [
    `/preview/${name}.sse`,
    [`../doc/contract/stream_${name}.sse`, "text/event-stream; charset=utf-8"],
  ]),
]);

function requestPath(req, res) {
  try {
    const url = new URL(req.url, "http://localhost");
    if (
      !req.url.startsWith("/") ||
      req.url.startsWith("//") ||
      url.origin !== "http://localhost"
    )
      throw new TypeError("Invalid request target");
    return url.pathname;
  } catch {
    res.writeHead(400, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("잘못된 요청 경로입니다.");
    return null;
  }
}

function createServer(backendPort = 8100) {
  return http.createServer(async (req, res) => {
    const pathname = requestPath(req, res);
    if (pathname === null) return;
    const apiMethod = {
      "/health": "GET",
      "/chat/stream": "GET",
      "/chat": "POST",
      "/sessions/reset": "POST",
    }[pathname];
    if (apiMethod) {
      if (req.method !== apiMethod) {
        res.writeHead(405, { Allow: apiMethod });
        res.end();
        return;
      }
      const upstream = http.request(
        {
          hostname: "127.0.0.1",
          port: backendPort,
          path: req.url,
          method: req.method,
          headers: {
            Accept: req.headers.accept || "application/json",
            "Content-Type": "application/json",
          },
        },
        (response) => {
          res.writeHead(response.statusCode, {
            "Content-Type":
              response.headers["content-type"] || "application/json",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
          });
          response.on("error", () => res.destroy());
          response.pipe(res);
        },
      );
      upstream.on("error", () => {
        if (res.headersSent) {
          res.destroy();
          return;
        }
        res.writeHead(502, {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "no-store",
        });
        res.end(
          JSON.stringify({
            code: "LLM_UNAVAILABLE",
            message:
              "백엔드 서버(127.0.0.1:8100)에 연결하지 못했습니다. 서버 실행 상태를 확인해주세요.",
          }),
        );
      });
      res.on("close", () => upstream.destroy());
      req.pipe(upstream);
      return;
    }
    if (!["GET", "HEAD"].includes(req.method)) {
      res.writeHead(405, { Allow: "GET, HEAD" });
      res.end();
      return;
    }
    const asset = /^\/assets\/[\w.-]+\.(js|css)$/.exec(pathname);
    const file = asset
      ? [
          "dist" + pathname,
          asset[1] === "js"
            ? "text/javascript; charset=utf-8"
            : "text/css; charset=utf-8",
        ]
      : files.get(pathname);
    if (!file) {
      res.writeHead(404);
      res.end("찾을 수 없는 경로입니다.");
      return;
    }
    try {
      const content = await readFile(join(__dirname, file[0]));
      res.writeHead(200, {
        "Content-Type": file[1],
        "Cache-Control": "no-store",
      });
      res.end(req.method === "HEAD" ? undefined : content);
    } catch {
      res.writeHead(asset ? 404 : 500);
      res.end(
        "프론트 빌드 파일을 읽지 못했습니다. npm run build를 실행해주세요.",
      );
    }
  });
}

if (require.main === module) {
  createServer().listen(8600, "127.0.0.1", () =>
    console.log("프론트 실행: http://127.0.0.1:8600"),
  );
}
module.exports = { createServer, requestPath };
