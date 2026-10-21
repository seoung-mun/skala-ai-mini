export function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function formatAnswerText(text, products = []) {
  let reply = (text || "").replace(/\[문서:\s*[^\]]+\]/g, "").trim();
  for (const product of products) {
    const label = `${product.bank || ""} ${product.product || ""}`.trim();
    if (!product.bank || !product.product) continue;
    const index = reply.indexOf(label);
    const prefix = reply.slice(reply.lastIndexOf("\n", index - 1) + 1, index);
    if (
      index > 0 &&
      prefix.replace(/\*\*/g, "").trim() &&
      !/^\s*(?:[-*]|\d+[.)])\s*$/.test(prefix)
    )
      reply = reply.slice(0, index).trimEnd() + "\n\n" + reply.slice(index);
  }
  return reply.replace(
    /(?<![\d.,])([0-9]+(?:,[0-9]{3})*)(\.[0-9]+)?(?=\s*(?:만|억)?\s*원)/g,
    (_, integer, fraction = "") =>
      integer.replace(/,/g, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",") +
      fraction,
  );
}

export function renderSimpleMarkdown(text) {
  if (!text) return "";
  let html = escapeHtml(text);
  // **bold**
  html = html.replace(
    /\*\*(.+?)\*\*/g,
    '<strong class="font-semibold text-secondary">$1</strong>',
  );
  // *italic*
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
  // `code`
  html = html.replace(
    /`(.+?)`/g,
    '<code class="bg-surface-container px-1 py-0.5 rounded font-label-mono text-[13px]">$1</code>',
  );
  return html
    .replace(
      /(^|\n)(?![-*] |\d+\. )([^\n]+)\n(?=(?:[-*] |\d+\. ))/g,
      "$1$2\n\n",
    )
    .split(/\n\s*\n/)
    .map((block) => {
      const lines = block.trim().split("\n");
      if (lines.every((line) => /^[-*] /.test(line))) {
        return `<ul>${lines.map((line) => `<li>${line.slice(2)}</li>`).join("")}</ul>`;
      }
      if (lines.every((line) => /^\d+\. /.test(line))) {
        return `<ol>${lines.map((line) => `<li>${line.replace(/^\d+\. /, "")}</li>`).join("")}</ol>`;
      }
      if (lines.length === 1 && /^#{1,3} /.test(block)) {
        return `<h3>${block.replace(/^#{1,3} /, "")}</h3>`;
      }
      // Older saved answers may contain one long paragraph with no Markdown.
      const readable =
        lines.length === 1 && block.length > 180
          ? block.replace(/([.!?])\s+(?=[가-힣A-Z0-9])/g, "$1\n\n")
          : block;
      return readable
        .split(/\n+/)
        .map((part) => `<p>${part.replace(/\n/g, "<br/>")}</p>`)
        .join("");
    })
    .join("");
}

export function formatNumber(num) {
  if (num === null || num === undefined) return "";
  return Number(num).toLocaleString("ko-KR");
}

export function formatTime(date) {
  const d = date || new Date();
  return (
    d.toLocaleTimeString("ko-KR", { timeZone: "Asia/Seoul", hour12: false }) +
    " KST"
  );
}

export function getSessionRecords(session) {
  let question = "";
  return (session?.messages || []).flatMap((message) => {
    if (message.role === "user") {
      question = message.content;
      return [];
    }
    if (message.role !== "assistant") return [];
    return [
      {
        question,
        message,
        products: message.finalAnswer?.products || [],
        metadata: message.finalAnswer?.metadata || message.metadata,
        source: message.source,
        events: (message.events || []).filter((event) =>
          [
            "RouteDecided",
            "PlanCreated",
            "TaskCompleted",
            "AnswerProgress",
            "ReplanCheck",
            "Validation",
          ].includes(event.type),
        ),
        citations: message.finalAnswer?.citations || [],
        error: message.error,
      },
    ];
  });
}

export async function consumeSseStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "",
    terminal = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) throw new Error("응답이 완료되기 전에 연결이 종료되었습니다.");
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop();
      for (const block of blocks) {
        const data = block
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (!data) continue;
        if (data === "[DONE]") {
          if (!terminal)
            throw new Error(
              "서버에서 최종 답변 또는 오류를 보내지 않았습니다.",
            );
          await reader.cancel();
          return;
        }
        let event;
        try {
          event = JSON.parse(data);
        } catch {
          throw new Error("서버 응답 형식이 올바르지 않습니다.");
        }
        if (
          ![
            "RouteDecided",
            "PlanCreated",
            "TaskCompleted",
            "AnswerProgress",
            "ReplanCheck",
            "Validation",
            "FinalAnswer",
            "Error",
          ].includes(event.type)
        )
          continue;
        if (terminal)
          throw new Error("종료된 응답 뒤에 추가 이벤트가 수신되었습니다.");
        terminal = event.type === "FinalAnswer" || event.type === "Error";
        onEvent(event);
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export function buildSessionExport(session) {
  let text = `# FinAgent 대화 기록 - ${session.title}\n\n- 대화 번호: ${session.id}\n- 내보낸 시각: ${new Date().toISOString()}\n`;
  for (const message of session.messages) {
    if (message.role === "user") {
      text += `\n## 질문\n${message.content}\n`;
      continue;
    }
    text += `\n## 답변${message.source === "mock" ? " (개발용 계약 예시)" : ""}\n${message.finalAnswer?.reply || message.error?.message || "응답 없음"}\n`;
    for (const citation of message.finalAnswer?.citations || [])
      text += `\n### 출처: ${citation.section}\n${citation.quote}\n상품 코드: ${citation.product_id}\n`;
    for (const [key, label] of [
      ["calculations", "계산 결과"],
      ["products", "조회 상품"],
      ["metadata", "실행 정보"],
    ]) {
      const value = message.finalAnswer?.[key];
      if (value && (!Array.isArray(value) || value.length))
        text += `\n### ${label}\n\x60\x60\x60json\n${JSON.stringify(value, null, 2)}\n\x60\x60\x60\n`;
    }
    if (message.events?.length)
      text += `\n### 분석 기록\n\x60\x60\x60json\n${JSON.stringify(message.events, null, 2)}\n\x60\x60\x60\n`;
    for (const warning of message.finalAnswer?.warnings || [])
      text += `\n유의사항: ${warning}\n`;
  }
  return text;
}

export function scrollMessages(canvas) {
  const latest = canvas?.querySelector(".chat-bubble-row:last-child");
  if (latest)
    canvas.scrollTop = Math.max(
      0,
      canvas.scrollTop +
        latest.getBoundingClientRect().top -
        canvas.getBoundingClientRect().top -
        24,
    );
}
