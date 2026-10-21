const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { before, after } = require("node:test");
const { createSSRApp } = require("vue");
const { renderToString } = require("@vue/server-renderer");
const html = readFileSync(join(__dirname, "index.html"), "utf8");
const context = {};
let FinalAnswer, AuditTrail;
before(async () => {
  const client = await import("./src/client.mjs");
  const { createServer } = await import("vite");
  const vite = await createServer({
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
    appType: "custom",
  });
  after(() => vite.close());
  ({ default: FinalAnswer } = await vite.ssrLoadModule(
    "/src/components/FinalAnswer.vue",
  ));
  ({ default: AuditTrail } = await vite.ssrLoadModule(
    "/src/components/AuditTrail.vue",
  ));
  Object.assign(context, {
    getRecords: client.getSessionRecords,
    exportSession: client.buildSessionExport,
    consumeStream: client.consumeSseStream,
    scrollMessages: () => client.scrollMessages(context.messageCanvas),
    messageCanvas: {},
  });
});
const render = (answer, events = []) =>
  renderToString(createSSRApp(FinalAnswer, { answer, events }));
const renderAudit = (answer, events = []) =>
  renderToString(
    createSSRApp(AuditTrail, {
      message: { finalAnswer: answer, route: answer.route, events },
    }),
  );
const finalEvent = (name) =>
  readFileSync(join(__dirname, "../doc/contract", name), "utf8")
    .split("\n")
    .filter((line) => line.startsWith("data: {"))
    .map((line) => JSON.parse(line.slice(6)))
    .find((event) => event.type === "FinalAnswer");

test("current contract calculation displays amount and supplied pre/post-tax results", async () => {
  const output = await render(finalEvent("stream_hard.sse"), [
    { type: "Validation", verdict: "pass" },
  ]);
  for (const value of ["500,000", "79,625", "67,362", "연 2.45"])
    assert.ok(output.includes(value));
  assert.ok(!output.includes("99.8%"));
});

test("legacy calculation and zero values remain visible without invented results", async () => {
  const output = await render({
    reply: "예시",
    calculations: [
      {
        inputs: { monthly_won: 0, rate_pct: 0, term_months: 12 },
        result: 0,
        unit: "원",
      },
    ],
  });
  assert.match(output, /월 납입금/);
  assert.match(output, /연 0/);
  assert.doesNotMatch(output, /세후 이자/);
  assert.doesNotMatch(output, /검증 통과 기록 없음/);
});

test("only the latest validation determines the badge, baseline remains unverified", async () => {
  const output = await renderAudit(finalEvent("stream_easy.sse"), [
    { type: "Validation", verdict: "pass" },
    { type: "Validation", verdict: "fail" },
  ]);
  assert.match(output, /검증 통과 기록 없음/);
  const baseline = await renderAudit(finalEvent("stream_v1.sse"));
  assert.match(baseline, /별도 검증 없음/);
  assert.doesNotMatch(baseline, /v1|v2/);
});

test("untrusted reply, product, formula, warning and citation text are escaped", async () => {
  const payload = "<img src=x onerror=alert(1)>";
  const output = await render({
    reply: payload,
    products: [{ product: payload }],
    calculations: [{ formula: payload }],
    citations: [{ quote: payload }],
    warnings: [payload],
  });
  assert.ok(!output.includes("<img"));
  assert.ok(output.includes("&lt;img"));
  assert.ok(!output.includes("undefined"));
});

test("Vue root template compiles and uses unique static IDs", async () => {
  const ids = [...html.matchAll(/\bid="([^"\n]+)"/g)].map((match) => match[1]);
  assert.equal(ids.length, new Set(ids).size);
  const { parse, compileTemplate } = require("@vue/compiler-sfc");
  const { descriptor, errors } = parse(
    readFileSync(join(__dirname, "src/App.vue"), "utf8"),
  );
  assert.equal(errors.length, 0);
  assert.ok(descriptor.scriptSetup);
  const result = compileTemplate({
    source: descriptor.template.content,
    filename: "App.vue",
    id: "app-test",
  });
  assert.deepEqual(result.errors, []);
});

test("long answers stay visible from their beginning instead of jumping to the bottom", async () => {
  const canvas = context.messageCanvas;
  canvas.scrollTop = 0;
  canvas.scrollHeight = 1200;
  canvas.getBoundingClientRect = () => ({ top: 125 });
  const row = {
    getBoundingClientRect: () => ({ top: 125 + 144 - canvas.scrollTop }),
  };
  canvas.querySelector = () => row;
  context.scrollMessages();
  assert.ok(
    row.getBoundingClientRect().top >= canvas.getBoundingClientRect().top,
  );
  assert.equal(canvas.scrollTop, 120);
  // Repeated progress events must keep the same message anchored.
  context.scrollMessages();
  assert.equal(canvas.scrollTop, 120);
  canvas.querySelector = () => null;
  assert.doesNotThrow(() => context.scrollMessages());
});

test("analysis and citation views collect every answer under its original question", async () => {
  const session = {
    messages: [
      { role: "user", content: "첫 질문" },
      {
        role: "assistant",
        events: [{ type: "TaskCompleted", ok: true }, { type: "FutureEvent" }],
        finalAnswer: { citations: [{ quote: "첫 원문" }] },
      },
      { role: "user", content: "두 번째 질문" },
      { role: "assistant", events: [], error: { message: "연결 오류" } },
    ],
  };
  const records = context.getRecords(session);
  assert.equal(records.length, 2);
  assert.equal(records[0].question, "첫 질문");
  assert.equal(records[0].events.length, 1);
  assert.equal(records[0].citations[0].quote, "첫 원문");
  assert.equal(records[1].question, "두 번째 질문");
  assert.equal(records[1].error.message, "연결 오류");
  assert.equal(context.getRecords({ messages: [] }).length, 0);
});

test("download contains real citations, analysis and a visible label for preview history", async () => {
  const text = context.exportSession({
    id: "test",
    title: "질문",
    messages: [
      {
        role: "assistant",
        source: "mock",
        events: [{ type: "Validation", verdict: "pass" }],
        finalAnswer: {
          reply: "답변",
          citations: [
            {
              section: "가입 조건",
              quote: "공시 원문",
              product_id: "product-1",
            },
          ],
          warnings: ["유의사항 문구"],
        },
      },
    ],
  });
  for (const value of [
    "개발용 계약 예시",
    "공시 원문",
    "가입 조건",
    "product-1",
    "Validation",
    "유의사항 문구",
  ])
    assert.ok(text.includes(value));
});

function streamResponse(text) {
  const bytes = new TextEncoder().encode(text);
  return new Response(
    new ReadableStream({
      start(controller) {
        // Split inside UTF-8 characters and SSE delimiters.
        for (let offset = 0; offset < bytes.length; offset += 3)
          controller.enqueue(bytes.slice(offset, offset + 3));
        controller.close();
      },
    }),
  );
}

test("real SSE reader supports fragmented Korean data, CRLF and additive event types", async () => {
  const events = [];
  const stream =
    'data: {"type":"FutureEvent"}\r\n\r\ndata: {"type":"FinalAnswer","reply":"한국어 답변"}\r\n\r\ndata: [DONE]\r\n\r\n';
  await context.consumeStream(streamResponse(stream), (event) =>
    events.push(event),
  );
  assert.equal(events.length, 1);
  assert.equal(events[0].reply, "한국어 답변");
});

test("truncated, malformed and missing-answer streams produce explicit errors", async () => {
  for (const stream of [
    'data: {"type":"FinalAnswer"}\n\n',
    "data: nope\n\n",
    "data: [DONE]\n\n",
  ]) {
    await assert.rejects(
      context.consumeStream(streamResponse(stream), async () => {}),
    );
  }
});

test("loan calculation uses the backend monthly repayment value", async () => {
  const output = await render({
    reply: "대출 계산",
    calculations: [
      {
        inputs: { product_type: "loan", amount_won: 10000000 },
        result: 850000,
        unit: "원",
      },
    ],
  });
  assert.match(output, /월 상환액/);
  assert.match(output, /850,000/);
  assert.doesNotMatch(output, /세전 이자/);
});

test("every published contract fixture passes the SSE reader", async () => {
  for (const name of ["easy", "hard", "error", "v1"]) {
    const events = [];
    await context.consumeStream(
      streamResponse(
        readFileSync(
          join(__dirname, "../doc/contract/stream_" + name + ".sse"),
          "utf8",
        ),
      ),
      (event) => events.push(event),
    );
    assert.ok(["FinalAnswer", "Error"].includes(events.at(-1).type), name);
  }
});

test("goal calculation labels target and required amounts without calling them interest", async () => {
  for (const [product_type, name, inputLabel] of [
    ["saving", "목표 달성 월 납입액", "목표 세후 만기 수령액"],
    ["deposit", "목표 달성 필요 원금", "목표 세후 만기 수령액"],
    ["loan", "최대 대출 원금", "월 상환 가능액"],
  ]) {
    const output = await render({
      reply: "역산 답변",
      calculations: [
        {
          name,
          inputs: { mode: "goal", product_type, amount_won: 10000000 },
          result: 826000,
          unit: "원",
        },
      ],
    });
    assert.match(output, new RegExp(inputLabel));
    assert.match(output, new RegExp(name));
    assert.match(output, /826,000/);
    assert.doesNotMatch(output, /세전 이자|세후 이자|월 상환액/);
  }
});

test("answer paragraphs and lists preserve financial values and escape content", async () => {
  const { renderSimpleMarkdown } = await import("./src/client.mjs");
  const output = renderSimpleMarkdown(
    "### 비교 결과\n\n- **가은행**: 연 3.94%\n- <script>alert(1)</script>\n\n세전 2,127,600원. 세후 1,799,949원.",
  );
  assert.match(output, /<h3>비교 결과<\/h3>/);
  assert.equal((output.match(/<li>/g) || []).length, 2);
  assert.ok(output.includes("3.94%") && output.includes("1,799,949원"));
  assert.ok(!output.includes("<script>"));
  const dense = renderSimpleMarkdown("긴 상품 설명입니다. ".repeat(25));
  assert.ok((dense.match(/<p>/g) || []).length > 1);
  const answer = await render({
    reply: "결과입니다. [문서: product-1]",
    products: [{ id: "product-1", bank: "가은행", product: "예금" }],
  });
  assert.doesNotMatch(answer, /\[문서:|\[근거:|product-1/);
});

test("audit distinguishes writing, validating and a rewrite while pending", async () => {
  for (const [stage, attempt, expected] of [
    ["writing", 1, "답변 작성 중"],
    ["validating", 1, "수치 검증 중"],
    ["writing", 2, "답변 다시 작성 시작"],
  ]) {
    const message = {
      pending: true,
      events: [{ type: "AnswerProgress", stage, attempt, writing_ms: 4200 }],
    };
    const output = await renderToString(createSSRApp(AuditTrail, { message }));
    assert.ok(output.includes(expected));
    assert.ok(!output.includes("응답 완료"));
    if (stage === "validating") assert.ok(output.includes("4.2초"));
  }
});

test("SSE keeps answer progress without exposing an unfinished answer", async () => {
  const response = new Response(
    'data: {"type":"AnswerProgress","stage":"writing","attempt":1}\n\ndata: {"type":"FinalAnswer","reply":"완료"}\n\ndata: [DONE]\n\n',
  );
  const events = [];
  await context.consumeStream(response, (event) => events.push(event));
  assert.deepEqual(
    events.map((e) => e.type),
    ["AnswerProgress", "FinalAnswer"],
  );
  assert.equal(events[0].reply, undefined);
});

test("conversation recall never advertises a financial validation pass", async () => {
  const output = await render({
    route: "chat",
    reply: "직전에는 예금 비교를 질문하셨어요.",
    products: [],
  });
  assert.doesNotMatch(output, /금융 수치 검증|검사 통과/);
  assert.doesNotMatch(output, /수치·요청 항목 검사 통과/);
});

test("chat contains only answer and cards, analysis retains warnings, formulas and metadata", async () => {
  const answer = {
    reply: "대화 본문",
    products: [{ id: "p", bank: "은행", product: "상품" }],
    calculations: [{ name: "이자", result: 123, formula: "원금 × 금리" }],
    citations: [{ product_id: "p", section: "가입 대상", quote: "원문 인용" }],
    warnings: ["공시 주의 문구"],
    metadata: {
      latency_ms: 1234,
      writing_runs: [
        {
          writing_ms: 1200,
          prompt_tokens: 10,
          completion_tokens: 20,
          gen_ms: 1000,
        },
      ],
    },
  };
  const chat = await render(answer);
  assert.match(chat, /대화 본문/);
  assert.match(chat, /상품/);
  assert.doesNotMatch(
    chat,
    /공시 주의 문구|원문 인용|원금 × 금리|응답 실행 정보|검사 통과/,
  );
  const analysis = await renderAudit(answer);
  assert.match(analysis, /공시 주의 문구/);
  assert.match(analysis, /원금 × 금리/);
  assert.match(analysis, /1,234/);
  const records = context.getRecords({
    messages: [{ role: "assistant", finalAnswer: answer }],
  });
  assert.equal(records[0].products[0].id, "p");
  assert.equal(records[0].citations[0].quote, "원문 인용");
  const noQuotes = context.getRecords({
    messages: [
      { role: "assistant", finalAnswer: { products: answer.products } },
    ],
  });
  assert.equal(noQuotes[0].products.length, 1);
  assert.equal(noQuotes[0].citations.length, 0);
  const root = readFileSync(join(__dirname, "src/App.vue"), "utf8");
  const conversation = root.slice(
    root.indexOf('id="message-canvas"'),
    root.indexOf('id="session-records"'),
  );
  assert.doesNotMatch(conversation, /<audit-trail/);
});

test("analysis escapes formulas and warnings moved out of conversation", async () => {
  const payload = "<img src=x onerror=alert(1)>";
  const output = await renderAudit({
    calculations: [{ formula: payload }],
    warnings: [payload],
  });
  assert.doesNotMatch(output, /<img/);
  assert.match(output, /&lt;img/);
});

test("product paragraphs and won separators improve the body without changing values", async () => {
  const { formatAnswerText, renderSimpleMarkdown } =
    await import("./src/client.mjs");
  const products = [
    { bank: "가은행", product: "가예금" },
    { bank: "나은행", product: "나예금" },
  ];
  const text = formatAnswerText(
    "가은행 가예금 3.94%, 세전 2127600원, 세후 1799949원. 나은행 나예금 3.42%, 가입대상: 개인.",
    products,
  );
  assert.match(text, /2,127,600원/);
  assert.match(text, /1,799,949원/);
  assert.match(text, /\n\n나은행 나예금/);
  assert.match(text, /3.94%/);
  assert.equal(
    formatAnswerText("원금 27,000,000원 · 2700만원 · 1234.50원 · 0원"),
    "원금 27,000,000원 · 2,700만원 · 1,234.50원 · 0원",
  );
  assert.equal(formatAnswerText(text, products), text);
  const markup = renderSimpleMarkdown("가은행 가예금\n나은행 나예금");
  assert.equal((markup.match(/<p>/g) || []).length, 2);
  const bullets = renderSimpleMarkdown(
    formatAnswerText("- 가은행 가예금\n- 나은행 나예금", products),
  );
  assert.equal((bullets.match(/<li>/g) || []).length, 2);
  const output = await render({ reply: text, products });
  assert.match(output, /2,127,600원/);
  assert.doesNotMatch(output, /<script>/);
});
