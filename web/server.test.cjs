const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { createServer } = require("./server.js");

async function listen(server, t) {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => {
    server.closeAllConnections();
    return new Promise((resolve) => server.close(resolve));
  });
  return server.address().port;
}

test("frontend serves only public assets and returns explicit backend connection errors", async (t) => {
  const placeholder = http.createServer();
  const unavailable = await listen(placeholder, t);
  await new Promise((resolve) => placeholder.close(resolve));
  const port = await listen(createServer(unavailable), t);
  const origin = `http://127.0.0.1:${port}`;
  assert.equal((await fetch(origin)).status, 200);
  const html = await (await fetch(origin)).text();
  const assets = [...html.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map(
    (match) => match[1],
  );
  assert.ok(assets.some((path) => path.endsWith(".js")));
  assert.ok(assets.some((path) => path.endsWith(".css")));
  for (const path of assets) {
    const response = await fetch(origin + path);
    assert.equal(response.status, 200, path);
    assert.match(
      response.headers.get("content-type"),
      path.endsWith(".js") ? /javascript/ : /css/,
    );
  }
  for (const scenario of ["easy", "hard", "error", "v1"]) {
    const response = await fetch(origin + "/preview/" + scenario + ".sse");
    assert.equal(response.status, 200);
    assert.match(await response.text(), /data: \[DONE\]/);
  }
  for (const path of [
    "/server.js",
    "/src/App.vue",
    "/src/client.mjs",
    "/vite.config.mjs",
    "/app.test.cjs",
    "/package.json",
    "/.env",
    "/%2e%2e/.env",
  ])
    assert.equal((await fetch(origin + path)).status, 404);
  const response = await fetch(origin + "/health");
  assert.equal(response.status, 502);
  assert.equal((await response.json()).code, "LLM_UNAVAILABLE");
  assert.equal((await fetch(origin + "/chat", { method: "GET" })).status, 405);
});

test("proxy preserves request parameters, POST bodies, HTTP errors and incremental SSE", async (t) => {
  const requests = [];
  let finish;
  const upstream = http.createServer(async (req, res) => {
    requests.push(req.url);
    if (req.url.startsWith("/chat/stream")) {
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      res.write('data: {"type":"RouteDecided","route":"easy"}\n\n');
      await new Promise((resolve) => {
        finish = resolve;
      });
      res.end(
        'data: {"type":"FinalAnswer","reply":"실제 전송"}\n\ndata: [DONE]\n\n',
      );
      return;
    }
    if (req.url === "/chat") {
      let body = "";
      for await (const chunk of req) body += chunk;
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          code: "BAD_REQUEST",
          message: JSON.parse(body).message,
        }),
      );
      return;
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
  });
  const backendPort = await listen(upstream, t);
  const port = await listen(createServer(backendPort), t);
  const origin = `http://127.0.0.1:${port}`;
  assert.equal((await (await fetch(origin + "/health")).json()).status, "ok");
  const post = await fetch(origin + "/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: "검증 질문" }),
  });
  assert.equal(post.status, 400);
  assert.equal((await post.json()).message, "검증 질문");
  const response = await fetch(
    origin + "/chat/stream?message=test&thread_id=t-7",
  );
  const reader = response.body.getReader();
  const first = new TextDecoder().decode((await reader.read()).value);
  assert.match(first, /RouteDecided/);
  assert.ok(!first.includes("FinalAnswer"));
  finish();
  let rest = "";
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    rest += new TextDecoder().decode(chunk.value);
  }
  assert.match(rest, /FinalAnswer/);
  assert.match(rest, /\[DONE\]/);
  assert.ok(requests.includes("/chat/stream?message=test&thread_id=t-7"));
});

test("Vite serves compiled SFCs and fixtures while keeping private files inaccessible", async (t) => {
  const { createServer } = await import("vite");
  const vite = await createServer({
    server: { port: 0, watch: null, ws: false },
  });
  t.after(() => vite.close());
  await vite.listen();
  const origin = `http://127.0.0.1:${vite.httpServer.address().port}`;
  for (const path of [
    "/",
    "/src/main.js",
    "/src/App.vue",
    "/src/components/FinalAnswer.vue",
    "/@id/__x00__plugin-vue:export-helper",
  ]) {
    const response = await fetch(origin + path);
    assert.equal(response.status, 200, path);
    if (path.endsWith(".vue"))
      assert.match(response.headers.get("content-type"), /javascript/);
  }
  for (const path of [
    "/server.js",
    "/package.json",
    "/app.test.cjs",
    "/.env",
    "/%2e%2e/.env",
  ])
    assert.equal((await fetch(origin + path)).status, 404, path);
  const fixture = await fetch(origin + "/preview/easy.sse");
  assert.equal(fixture.status, 200);
  assert.match(await fixture.text(), /data: \[DONE\]/);
});

test("session reset proxy preserves the thread and rejects other methods", async (t) => {
  const upstream = http.createServer(async (req, res) => {
    assert.equal(req.url, "/sessions/reset");
    assert.equal(req.method, "POST");
    let body = "";
    for await (const chunk of req) body += chunk;
    assert.equal(JSON.parse(body).thread_id, "t-clear");
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true, thread_id: "t-clear" }));
  });
  const origin = `http://127.0.0.1:${await listen(createServer(await listen(upstream, t)), t)}`;
  const response = await fetch(origin + "/sessions/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: "t-clear" }),
  });
  assert.deepEqual(await response.json(), { ok: true, thread_id: "t-clear" });
  assert.equal((await fetch(origin + "/sessions/reset")).status, 405);
});
