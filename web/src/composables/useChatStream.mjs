import { ref, onUnmounted } from "vue";
import { formatTime, consumeSseStream } from "../client.mjs";

export function useChatStream(session, save, streaming, preview) {
  const input = ref("");
  let controller;
  async function send(text = input.value) {
    const question = text.trim();
    if (!question || streaming.value) return;
    if (question.length > 500) {
      alert("메시지는 최대 500자까지 입력 가능합니다.");
      return;
    }
    input.value = "";
    const current = session.value;
    if (!current.messages.length) current.title = question;
    current.messages.push({
      role: "user",
      content: question,
      timestamp: formatTime(),
    });
    current.messages.push({
      role: "assistant",
      question,
      route: null,
      events: [],
      finalAnswer: null,
      error: null,
      source: preview ? "mock" : "live",
      pending: true,
    });
    // Access through Vue's proxy so incoming SSE events update the components.
    const answer = current.messages.at(-1);
    streaming.value = true;
    controller = new AbortController();
    save();
    try {
      const url = preview
        ? `/preview/${preview}.sse`
        : `/chat/stream?${new URLSearchParams({ message: question, thread_id: current.id })}`;
      const response = await fetch(url, {
        signal: controller.signal,
        headers: { Accept: "text/event-stream" },
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw Object.assign(
          new Error(error.message || `서버 응답 오류 (${response.status})`),
          { code: error.code },
        );
      }
      await consumeSseStream(response, (event) => {
        answer.events.push(event);
        if (event.type === "RouteDecided") answer.route = event.route;
        if (event.type === "FinalAnswer") {
          answer.revealAnswer = true;
          answer.finalAnswer = event;
          answer.route = event.route;
          answer.metadata = event.metadata;
        }
        if (event.type === "Error") answer.error = event;
      });
    } catch (error) {
      answer.error =
        error.name === "AbortError"
          ? {
              code: "CANCELLED",
              message: "사용자에 의해 스트리밍이 중단되었습니다.",
            }
          : {
              code: error.code || "CONNECTION_FAILED",
              message:
                error.message || "연결 상태를 확인한 뒤 다시 요청해주세요.",
            };
    } finally {
      answer.pending = false;
      streaming.value = false;
      controller = null;
      save();
    }
  }
  function retry(index) {
    const previous = session.value.messages
      .slice(0, index)
      .findLast((message) => message.role === "user");
    if (previous) send(previous.content);
  }

  const stop = () => controller?.abort();
  onUnmounted(stop);
  return { input, send, retry, stop };
}
