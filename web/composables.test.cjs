const { test, before } = require("node:test");
const assert = require("node:assert/strict");
const { createRenderer, ref, watchEffect, nextTick } = require("vue");
globalThis.window = new EventTarget();

let useSessions, useChatStream, useServiceStatus;
before(async () => {
  ({ useSessions } = await import("./src/composables/useSessions.mjs"));
  ({ useChatStream } = await import("./src/composables/useChatStream.mjs"));
  ({ useServiceStatus } =
    await import("./src/composables/useServiceStatus.mjs"));
});

function mount(t, setup) {
  let state;
  const renderer = createRenderer({
    createComment: () => ({}),
    insert() {},
    remove() {},
    parentNode: () => null,
    nextSibling: () => null,
  });
  const app = renderer.createApp({
    setup() {
      state = setup();
      return () => null;
    },
  });
  app.mount({});
  let mounted = true;
  const unmount = () => {
    if (mounted) {
      app.unmount();
      mounted = false;
    }
  };
  t.after(unmount);
  return { state, unmount };
}

function storage(t, value = "[]") {
  const descriptor = Object.getOwnPropertyDescriptor(
    globalThis,
    "localStorage",
  );
  const writes = [];
  const values = new Map([["finagent_sessions_v1", value]]);
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      get length() {
        return values.size;
      },
      key: (index) => [...values.keys()][index] ?? null,
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, text) => {
        writes.push({ key, text });
        values.set(key, text);
      },
      removeItem: (key) => values.delete(key),
    },
  });
  t.after(() => {
    if (descriptor)
      Object.defineProperty(globalThis, "localStorage", descriptor);
    else delete globalThis.localStorage;
  });
  writes.records = () =>
    [...values]
      .filter(([key]) => key.startsWith("finagent_session_v2:"))
      .map(([, raw]) => JSON.parse(raw));
  writes.notify = (id) => {
    const event = new Event("storage");
    event.key = "finagent_session_v2:" + id;
    event.storageArea = localStorage;
    window.dispatchEvent(event);
  };
  return writes;
}

function response(events) {
  return new Response(
    events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("") +
      "data: [DONE]\n\n",
  );
}

test("session recovery keeps the original corrupt storage and reports the problem", (t) => {
  const writes = storage(t, "not json");
  const { state } = mount(t, () => useSessions(ref(false)));
  assert.equal(state.sessions.value.length, 1);
  assert.equal(state.session.value.messages.length, 0);
  assert.match(state.storageError.value, /원본 저장 기록을 보존/);
  assert.equal(writes.length, 0);
});

test("restored pending answers become interrupted; stream locks protect session changes", async (t) => {
  storage(
    t,
    JSON.stringify([
      {
        id: "old",
        title: "이전 질문",
        messages: [
          { role: "user", content: "질문" },
          { role: "assistant", pending: true },
        ],
      },
    ]),
  );
  const streaming = ref(false);
  const { state } = mount(t, () => useSessions(streaming));
  const freshId = state.currentId.value;
  assert.notEqual(freshId, "old");
  assert.deepEqual(state.session.value.messages, []);
  state.selectSession("old");
  assert.equal(state.session.value.messages[1].pending, false);
  assert.equal(state.session.value.messages[1].error.code, "INTERRUPTED");
  streaming.value = true;
  state.newSession();
  await state.deleteSession("old");
  state.selectSession("other");
  assert.equal(state.sessions.value.length, 2);
  assert.equal(state.currentId.value, "old");
  streaming.value = false;
  const id = freshId;
  state.selectSession("old");
  await state.deleteSession("old");
  assert.equal(state.currentId.value, id);
  await state.deleteSession(id);
  assert.equal(state.sessions.value.length, 1);
  assert.notEqual(state.currentId.value, id);
});

test("SSE updates stay reactive and keep the session ID, final metadata and storage", async (t) => {
  storage(t);
  const streaming = ref(false);
  let request, observed;
  t.mock.method(globalThis, "fetch", async (url) => {
    request = new URL(url, "http://localhost");
    return response([
      { type: "RouteDecided", route: "hard" },
      {
        type: "FinalAnswer",
        route: "hard",
        reply: "계산 결과",
        metadata: { llm_calls: 2 },
      },
    ]);
  });
  const { state } = mount(t, () => {
    const sessions = useSessions(streaming);
    const chat = useChatStream(
      sessions.session,
      sessions.save,
      streaming,
      null,
    );
    watchEffect(() => {
      observed = sessions.session.value?.messages.at(-1)?.finalAnswer?.reply;
    });
    return { ...sessions, ...chat };
  });
  state.input.value = "이자 얼마야?";
  await state.send();
  await nextTick();
  assert.equal(request.searchParams.get("message"), "이자 얼마야?");
  assert.equal(request.searchParams.get("thread_id"), state.currentId.value);
  assert.equal(state.input.value, "");
  assert.equal(streaming.value, false);
  assert.equal(observed, "계산 결과");
  const answer = state.session.value.messages.at(-1);
  assert.equal(answer.source, "live");
  assert.equal(answer.pending, false);
  assert.equal(answer.metadata.llm_calls, 2);
  state.retry(1);
  await new Promise(setImmediate);
  assert.equal(state.session.value.messages.length, 4);
});

test("preview remains labelled as mock and explicit stop releases the streaming lock", async (t) => {
  const session = ref({ id: "preview", messages: [] });
  const streaming = ref(false);
  let requested;
  t.mock.method(globalThis, "fetch", async (url, { signal }) => {
    requested = url;
    return new Response(
      new ReadableStream({
        start(controller) {
          signal.addEventListener("abort", () => {
            controller.error(new DOMException("Stopped", "AbortError"));
          });
        },
      }),
    );
  });
  const { state } = mount(t, () =>
    useChatStream(session, () => {}, streaming, "hard"),
  );
  const pending = state.send("계약 예시");
  assert.equal(streaming.value, true);
  await state.send("중복 요청");
  assert.equal(session.value.messages.length, 2);
  state.stop();
  await pending;
  assert.equal(requested, "/preview/hard.sse");
  assert.equal(session.value.messages.at(-1).source, "mock");
  assert.equal(session.value.messages.at(-1).error.code, "CANCELLED");
  assert.equal(session.value.messages.at(-1).pending, false);
  assert.equal(streaming.value, false);
});

test("health polling exposes failures and cleans up requests and timers on unmount", async (t) => {
  const timers = [],
    cleared = [],
    signals = [];
  t.mock.method(globalThis, "setInterval", (fn, delay) => {
    timers.push({ fn, delay });
    return timers.length;
  });
  t.mock.method(globalThis, "clearInterval", (id) => cleared.push(id));
  t.mock.method(globalThis, "fetch", async (url, { signal }) => {
    signals.push(signal);
    return new Response(
      JSON.stringify({ status: "ok", data_month: "202609", llm: "test-model" }),
    );
  });
  const { state, unmount } = mount(t, useServiceStatus);
  await new Promise(setImmediate);
  assert.equal(state.health.online, true);
  assert.equal(state.month.value, "2026.09");
  assert.equal(state.health.llm, "test-model");
  assert.deepEqual(
    timers.map((timer) => timer.delay),
    [1000, 8000],
  );
  t.mock.method(globalThis, "fetch", async (url, { signal }) => {
    signals.push(signal);
    throw new Error("offline");
  });
  await state.checkHealth();
  assert.equal(state.health.online, false);
  assert.equal(state.health.llm, null);
  unmount();
  assert.equal(signals.at(-1).aborted, true);
  assert.deepEqual(cleared, [1, 2]);
});

test("verified text reveals progressively, finishes immediately on request, and clears timers", async (t) => {
  const { useProgressiveText } =
    await import("./src/composables/useProgressiveText.mjs");
  let now = 0,
    tick;
  const cleared = [];
  t.mock.method(Date, "now", () => now);
  t.mock.method(globalThis, "setInterval", (fn) => {
    tick = fn;
    return 1;
  });
  t.mock.method(globalThis, "clearInterval", (id) => cleared.push(id));
  const text = ref("검증 완료된 답변입니다. 이자는 1,799,949원입니다.");
  const { state, unmount } = mount(t, () =>
    useProgressiveText(text, ref(true)),
  );
  assert.equal(state.visible.value, "");
  assert.equal(state.revealing.value, true);
  now = 100;
  tick();
  assert.ok(
    state.visible.value.length > 0 &&
      state.visible.value.length < text.value.length,
  );
  assert.ok(text.value.startsWith(state.visible.value));
  state.finish();
  assert.equal(state.visible.value, text.value);
  assert.equal(state.revealing.value, false);
  text.value = "새 답변";
  await nextTick();
  now += 500;
  tick();
  assert.equal(state.visible.value, "새 답변");
  unmount();
  assert.ok(cleared.includes(1));
});

test("history and reduced-motion answers are visible immediately without playback", async (t) => {
  const { useProgressiveText } =
    await import("./src/composables/useProgressiveText.mjs");
  const original = globalThis.matchMedia;
  globalThis.matchMedia = () => ({ matches: true });
  t.after(() => {
    if (original) globalThis.matchMedia = original;
    else delete globalThis.matchMedia;
  });
  let timers = 0;
  t.mock.method(globalThis, "setInterval", () => {
    timers++;
  });
  for (const enabled of [false, true]) {
    const { state } = mount(t, () =>
      useProgressiveText(ref("과거 답변 전체"), ref(enabled)),
    );
    assert.equal(state.visible.value, "과거 답변 전체");
    assert.equal(state.revealing.value, false);
  }
  assert.equal(timers, 0);
});

test("every page entry starts a fresh session and preserves existing conversations", (t) => {
  const previous = {
    id: "saved",
    title: "기존 대화",
    messages: [{ role: "user", content: "이전 질문" }],
  };
  const writes = storage(t, JSON.stringify([previous]));
  const first = mount(t, () => useSessions(ref(false))).state;
  assert.notEqual(first.currentId.value, "saved");
  assert.deepEqual(first.session.value.messages, []);
  const firstId = first.currentId.value;
  first.selectSession("saved");
  assert.equal(first.session.value.messages[0].content, "이전 질문");
  const second = mount(t, () => useSessions(ref(false))).state;
  assert.notEqual(second.currentId.value, firstId);
  assert.deepEqual(second.session.value.messages, []);
  assert.deepEqual(
    second.sessions.value.find((s) => s.id === "saved"),
    previous,
  );
  const persisted = writes.records();
  assert.equal(persisted.length, 1);
  assert.notEqual(persisted[0].id, second.currentId.value);
  assert.ok(persisted.some((s) => s.id === "saved"));
});

test("empty drafts are absent from history and storage across refreshes and new-chat clicks", (t) => {
  const saved = {
    id: "saved",
    createdAt: new Date().toISOString(),
    messages: [{ role: "user", content: "질문" }],
  };
  const writes = storage(
    t,
    JSON.stringify([{ id: "empty", messages: [] }, saved]),
  );
  for (let i = 0; i < 3; i++) {
    const { state } = mount(t, () => useSessions(ref(false)));
    assert.deepEqual(
      state.history.value.map((s) => s.id),
      ["saved"],
    );
    for (let n = 0; n < 3; n++) state.newSession();
    assert.equal(state.sessions.value.length, 2);
    assert.equal(state.groups.value.flatMap((g) => g.items).length, 1);
    assert.deepEqual(writes.records(), [saved]);
  }
});

test("a first question enters history immediately even when the request fails", async (t) => {
  const writes = storage(t);
  t.mock.method(globalThis, "fetch", async () => {
    const stored = writes.records();
    assert.equal(stored.length, 1);
    assert.equal(stored[0].messages[0].content, "첫 질문");
    throw new Error("연결 실패");
  });
  const { state } = mount(t, () => {
    const streaming = ref(false);
    const sessions = useSessions(streaming);
    return {
      ...sessions,
      ...useChatStream(sessions.session, sessions.save, streaming, null),
    };
  });
  assert.deepEqual(writes.records(), []);
  await state.send("   ");
  assert.equal(state.history.value.length, 0);
  await state.send("첫 질문");
  assert.equal(state.history.value.length, 1);
  assert.equal(
    state.session.value.messages.at(-1).error.code,
    "CONNECTION_FAILED",
  );
  state.newSession();
  assert.equal(state.history.value.length, 1);
  assert.equal(writes.records().length, 1);
});

function confirmReset(t, result = true) {
  const previous = globalThis.confirm;
  globalThis.confirm = () => result;
  t.after(() => {
    if (previous) globalThis.confirm = previous;
    else delete globalThis.confirm;
  });
}

test("clear waits for server acknowledgement, resets its context and rotates the local thread", async (t) => {
  const writes = storage(t);
  confirmReset(t);
  let respond, request;
  t.mock.method(globalThis, "fetch", (url, options) => {
    request = { url, options };
    return new Promise((resolve) => {
      respond = resolve;
    });
  });
  const streaming = ref(false);
  const { state } = mount(t, () => useSessions(streaming));
  state.session.value.messages.push({ role: "user", content: "이전 질문" });
  state.save();
  const id = state.currentId.value;
  const pending = state.clearChat();
  await new Promise(setImmediate);
  assert.equal(streaming.value, true);
  assert.equal(state.session.value.messages.length, 1);
  state.newSession();
  assert.equal(state.currentId.value, id);
  assert.equal(request.url, "/sessions/reset");
  assert.equal(request.options.method, "POST");
  assert.deepEqual(JSON.parse(request.options.body), { thread_id: id });
  respond(new Response(JSON.stringify({ ok: true, thread_id: id })));
  await pending;
  assert.equal(streaming.value, false);
  assert.notEqual(state.currentId.value, id);
  assert.deepEqual(state.session.value.messages, []);
  assert.equal(state.history.value.length, 0);
  assert.deepEqual(writes.records(), []);
});

test("failed reset preserves the conversation and cancellation makes no request", async (t) => {
  storage(t);
  confirmReset(t, false);
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls++;
    return new Response(JSON.stringify({ message: "서버 초기화 실패" }), {
      status: 500,
    });
  });
  const { state } = mount(t, () => useSessions(ref(false)));
  state.session.value.messages.push({ role: "user", content: "보존 질문" });
  state.save();
  const id = state.currentId.value;
  await state.clearChat();
  assert.equal(calls, 0);
  globalThis.confirm = () => true;
  await state.clearChat();
  assert.equal(calls, 1);
  assert.equal(state.currentId.value, id);
  assert.equal(state.session.value.messages[0].content, "보존 질문");
  assert.match(state.storageError.value, /대화 초기화 실패/);
});

test("preview reset remains usable without a backend", async (t) => {
  storage(t);
  confirmReset(t);
  t.mock.method(globalThis, "fetch", () => {
    throw new Error("preview must not call server");
  });
  const { state } = mount(t, () => useSessions(ref(false), true));
  state.session.value.messages.push({ role: "user", content: "예시 질문" });
  const id = state.currentId.value;
  await state.clearChat();
  assert.notEqual(state.currentId.value, id);
  assert.deepEqual(state.session.value.messages, []);
  assert.equal(state.storageError.value, "");
});

test("two tabs keep separate conversations and synchronize updates and deletion", async (t) => {
  const store = storage(t);
  const first = mount(t, () => useSessions(ref(false))).state;
  const second = mount(t, () => useSessions(ref(false))).state;
  const firstId = first.currentId.value,
    secondId = second.currentId.value;
  for (const [tab, question] of [
    [first, "A 질문"],
    [second, "B 질문"],
  ]) {
    await tab.editSession(() => {
      tab.session.value.messages.push({ role: "user", content: question });
      tab.save();
    });
  }
  assert.equal(store.records().length, 2);
  store.notify(firstId);
  store.notify(secondId);
  assert.equal(first.history.value.length, 2);
  assert.equal(second.history.value.length, 2);
  await first.editSession(() => {
    first.session.value.messages.push({ role: "user", content: "A 후속 질문" });
    first.save();
  });
  store.notify(firstId);
  second.selectSession(firstId);
  assert.equal(second.session.value.messages.at(-1).content, "A 후속 질문");
  await first.deleteSession(firstId);
  store.notify(firstId);
  assert.equal(second.currentId.value, secondId);
  assert.deepEqual(
    store.records().map((item) => item.id),
    [secondId],
  );
  const reloaded = mount(t, () => useSessions(ref(false))).state;
  assert.deepEqual(
    reloaded.history.value.map((item) => item.id),
    [secondId],
  );
});

test("migration preserves existing newer records and removed legacy sessions stay removed", async (t) => {
  const old = { id: "old", messages: [{ role: "user", content: "이전 질문" }] };
  const store = storage(t, JSON.stringify([old]));
  localStorage.setItem(
    "finagent_session_v2:old",
    JSON.stringify({ ...old, title: "새 제목" }),
  );
  const state = mount(t, () => useSessions(ref(false))).state;
  assert.equal(state.history.value[0].title, "새 제목");
  assert.equal(localStorage.getItem("finagent_sessions_v1"), null);
  await state.deleteSession("old");
  const next = mount(t, () => useSessions(ref(false))).state;
  assert.equal(next.history.value.length, 0);
  assert.deepEqual(store.records(), []);
});

test("failed migration and corrupt data retain their original recoverable storage", (t) => {
  const original = JSON.stringify([
    { id: "old", messages: [{ role: "user", content: "보존 질문" }] },
  ]);
  storage(t, original);
  t.mock.method(localStorage, "setItem", () => {
    throw new Error("quota exceeded");
  });
  const state = mount(t, () => useSessions(ref(false))).state;
  assert.equal(localStorage.getItem("finagent_sessions_v1"), original);
  assert.match(state.storageError.value, /원본 저장 기록을 보존/);
});

test("a conversation lock prevents another tab from sending or deleting during an active answer", async (t) => {
  const store = storage(t);
  let finish,
    calls = 0;
  t.mock.method(globalThis, "fetch", () => {
    calls++;
    return new Promise((resolve) => {
      finish = () =>
        resolve(response([{ type: "FinalAnswer", reply: "완료" }]));
    });
  });
  const setup = () => {
    const streaming = ref(false);
    const sessions = useSessions(streaming);
    return {
      ...sessions,
      ...useChatStream(
        sessions.session,
        sessions.save,
        streaming,
        null,
        sessions.editSession,
      ),
    };
  };
  const first = mount(t, setup).state;
  const id = first.currentId.value;
  const pending = first.send("첫 질문");
  await new Promise(setImmediate);
  const second = mount(t, setup).state;
  second.selectSession(id);
  assert.equal(store.records()[0].messages.at(-1).pending, true);
  await second.send("충돌 질문");
  await second.deleteSession(id);
  assert.equal(calls, 1);
  assert.match(second.storageError.value, /다른 탭/);
  assert.equal(store.records().length, 1);
  finish();
  await pending;
  const followup = second.send("후속 질문");
  await new Promise(setImmediate);
  assert.equal(calls, 2);
  assert.equal(second.session.value.messages.length, 4);
  assert.equal(second.session.value.messages[1].finalAnswer.reply, "완료");
  finish();
  await followup;
  assert.equal(store.records()[0].messages.length, 4);
});

test("a failed save cannot be overwritten by a refresh from another tab", async (t) => {
  const store = storage(t);
  const state = mount(t, () => useSessions(ref(false))).state;
  const id = state.currentId.value;
  state.session.value.messages.push({ role: "user", content: "저장된 질문" });
  state.save();
  t.mock.method(localStorage, "setItem", () => {
    throw new Error("quota exceeded");
  });
  state.session.value.messages.push({
    role: "user",
    content: "아직 저장 안 된 질문",
  });
  state.save();
  store.notify(id);
  let edited = false;
  await state.editSession(() => {
    edited = true;
  });
  assert.equal(edited, false);
  assert.equal(
    state.session.value.messages.at(-1).content,
    "아직 저장 안 된 질문",
  );
  assert.match(state.storageError.value, /내보낸 뒤 새로고침/);
});
