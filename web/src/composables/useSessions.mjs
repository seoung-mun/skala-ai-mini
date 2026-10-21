import { ref, computed, onMounted } from "vue";
import { buildSessionExport } from "../client.mjs";

export function useSessions(streaming, preview = false) {
  const sessions = ref([]);
  const currentId = ref(null);
  const storageError = ref("");
  const hasQuestion = (item) =>
    item.messages.some(
      (message) =>
        message.role === "user" &&
        typeof message.content === "string" &&
        message.content.trim(),
    );
  const history = computed(() => sessions.value.filter(hasQuestion));
  const session = computed(() =>
    sessions.value.find((item) => item.id === currentId.value),
  );
  const title = computed(
    () =>
      session.value?.messages.find((message) => message.role === "user")
        ?.content ||
      session.value?.title ||
      "새 금융 상담",
  );
  const groups = computed(() => {
    const today = new Date().toLocaleDateString("ko-KR", {
      timeZone: "Asia/Seoul",
    });
    const isToday = (item) =>
      new Date(item.createdAt).toLocaleDateString("ko-KR", {
        timeZone: "Asia/Seoul",
      }) === today;
    return [
      { label: "오늘", items: history.value.filter(isToday) },
      {
        label: "이전 기록",
        items: history.value.filter((item) => !isToday(item)),
      },
    ];
  });
  function save() {
    try {
      localStorage.setItem(
        "finagent_sessions_v1",
        JSON.stringify(history.value, (key, value) =>
          key === "revealAnswer" ? undefined : value,
        ),
      );
      storageError.value = "";
    } catch {
      storageError.value =
        "대화 기록을 저장하지 못했습니다. 저장 공간을 확인하고 필요한 대화를 내보내세요.";
    }
  }
  function newSession(persist = true) {
    if (streaming.value) return;
    const item = {
      id: "t-" + crypto.randomUUID(),
      title: "새 금융 상담",
      createdAt: new Date().toISOString(),
      messages: [],
    };
    sessions.value = [item, ...history.value];
    currentId.value = item.id;
    if (persist) save();
  }
  function selectSession(id) {
    if (streaming.value) return;
    currentId.value = id;
  }
  function deleteSession(id) {
    if (streaming.value) return;
    sessions.value = sessions.value.filter((item) => item.id !== id);
    if (!sessions.value.length) newSession(false);
    else if (currentId.value === id) currentId.value = sessions.value[0].id;
    save();
  }
  async function clearChat() {
    if (
      streaming.value ||
      !session.value ||
      !confirm("현재 대화 내용을 지우시겠습니까?")
    )
      return;
    const current = session.value;
    streaming.value = true;
    try {
      if (!preview) {
        const response = await fetch("/sessions/reset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ thread_id: current.id }),
        });
        const result = await response.json();
        if (
          !response.ok ||
          result.ok !== true ||
          result.thread_id !== current.id
        )
          throw new Error(
            result.message || "서버 초기화 응답을 확인하지 못했습니다.",
          );
      }
      current.id = "t-" + crypto.randomUUID();
      current.messages = [];
      current.title = "새 금융 상담";
      currentId.value = current.id;
      save();
    } catch (error) {
      storageError.value = `대화 초기화 실패: ${error.message} 기존 대화는 유지했습니다.`;
    } finally {
      streaming.value = false;
    }
  }
  function exportChat() {
    if (streaming.value || !session.value) return;
    const url = URL.createObjectURL(
      new Blob([buildSessionExport(session.value)], {
        type: "text/markdown;charset=utf-8",
      }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `finagent-${session.value.id}.md`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function loadSessions() {
    try {
      const stored = JSON.parse(
        localStorage.getItem("finagent_sessions_v1") || "[]",
      );
      if (
        !Array.isArray(stored) ||
        stored.some(
          (item) =>
            !item ||
            typeof item.id !== "string" ||
            !Array.isArray(item.messages),
        )
      )
        throw new Error("저장 형식 오류");
      sessions.value = stored.filter(hasQuestion);
      // An interrupted tab cannot resume its previous network stream.
      for (const item of stored)
        for (const message of item.messages)
          if (message.pending) {
            message.pending = false;
            message.error = {
              code: "INTERRUPTED",
              message:
                "페이지가 종료되어 응답이 중단되었습니다. 다시 요청해주세요.",
            };
          }
      newSession();
    } catch {
      storageError.value =
        "기존 대화 기록을 읽지 못했습니다. 원본 저장 기록을 보존했습니다.";
      newSession(false);
    }
  }

  onMounted(loadSessions);
  return {
    sessions,
    history,
    currentId,
    storageError,
    session,
    title,
    groups,
    save,
    newSession,
    selectSession,
    deleteSession,
    clearChat,
    exportChat,
  };
}
