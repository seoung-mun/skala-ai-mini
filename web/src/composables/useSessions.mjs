import { ref, computed, onMounted, onUnmounted } from "vue";
import { buildSessionExport } from "../client.mjs";

const storagePrefix = "finagent_session_v2:";
const legacyKey = "finagent_sessions_v1";
const serialize = (item) =>
  JSON.stringify(item, (key, value) =>
    key === "revealAnswer" ? undefined : value,
  );
function validateSession(item) {
  if (
    !item ||
    typeof item.id !== "string" ||
    !item.id ||
    (item.createdAt != null && typeof item.createdAt !== "string") ||
    !Array.isArray(item.messages) ||
    item.messages.some(
      (message) => !message || typeof message.role !== "string",
    )
  )
    throw new Error("저장 형식 오류");
  return item;
}
function recoverPending(item) {
  for (const message of item.messages)
    if (message.pending) {
      message.pending = false;
      message.error = {
        code: "INTERRUPTED",
        message:
          "이 탭에서 이전 응답을 이어 받을 수 없습니다. 다른 탭의 완료를 기다리거나 다시 요청해주세요.",
      };
    }
}

export function useSessions(streaming, preview = false) {
  const sessions = ref([]);
  const currentId = ref(null);
  const storageError = ref("");
  const savedIds = new Set();
  const unsavedIds = new Set();
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
      const current = session.value;
      if (!current || !hasQuestion(current)) return;
      localStorage.setItem(storagePrefix + current.id, serialize(current));
      savedIds.add(current.id);
      unsavedIds.delete(current.id);
      storageError.value = "";
    } catch {
      if (session.value) unsavedIds.add(session.value.id);
      storageError.value =
        "대화 기록을 저장하지 못했습니다. 저장 공간을 확인하고 필요한 대화를 내보내세요.";
    }
  }
  function newSession() {
    if (streaming.value) return;
    const item = {
      id: "t-" + crypto.randomUUID(),
      title: "새 금융 상담",
      createdAt: new Date().toISOString(),
      messages: [],
    };
    sessions.value = [item, ...history.value];
    currentId.value = item.id;
  }
  function selectSession(id) {
    if (streaming.value) return;
    currentId.value = id;
  }
  function removeLocal(id) {
    savedIds.delete(id);
    sessions.value = sessions.value.filter((item) => item.id !== id);
    if (!sessions.value.length) newSession();
    else if (currentId.value === id) currentId.value = sessions.value[0].id;
  }
  function refreshSession(id) {
    const raw = localStorage.getItem(storagePrefix + id);
    if (raw === null) {
      if (!savedIds.has(id)) return true;
      removeLocal(id);
      return false;
    }
    const item = validateSession(JSON.parse(raw));
    if (item.id !== id) throw new Error("대화 번호 불일치");
    savedIds.add(id);
    const index = sessions.value.findIndex((entry) => entry.id === id);
    if (index < 0) sessions.value.push(item);
    else sessions.value[index] = item;
    return true;
  }
  async function editSession(action, id = currentId.value) {
    if (streaming.value) return;
    if (unsavedIds.has(id)) {
      storageError.value =
        "저장하지 못한 대화가 있습니다. 현재 대화를 내보낸 뒤 새로고침해주세요.";
      return;
    }
    const selectedId = currentId.value;
    try {
      // One tab owns a conversation through its request and final save.
      return await navigator.locks.request(
        `finagent-session:${id}`,
        { ifAvailable: true },
        async (lock) => {
          if (!lock) {
            storageError.value =
              "다른 탭에서 이 대화를 처리 중입니다. 완료 후 다시 시도해주세요.";
            return;
          }
          if (currentId.value !== selectedId || streaming.value) return;
          if (!refreshSession(id)) return;
          const item = sessions.value.find((entry) => entry.id === id);
          if (item) recoverPending(item);
          return await action();
        },
      );
    } catch (error) {
      storageError.value = `대화 기록 처리 실패: ${error.message}`;
    }
  }
  function deleteSession(id) {
    return editSession(() => {
      localStorage.removeItem(storagePrefix + id);
      removeLocal(id);
      storageError.value = "";
    }, id);
  }
  async function clearChat() {
    if (
      streaming.value ||
      !session.value ||
      !confirm("현재 대화 내용을 지우시겠습니까?")
    )
      return;
    return editSession(async () => {
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
        localStorage.removeItem(storagePrefix + current.id);
        savedIds.delete(current.id);
        current.id = "t-" + crypto.randomUUID();
        current.messages = [];
        current.title = "새 금융 상담";
        currentId.value = current.id;
        storageError.value = "";
      } catch (error) {
        storageError.value = `대화 초기화 실패: ${error.message} 기존 대화는 유지했습니다.`;
      } finally {
        streaming.value = false;
      }
    });
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
      const legacy = localStorage.getItem(legacyKey);
      if (legacy !== null) {
        const stored = JSON.parse(legacy);
        if (!Array.isArray(stored)) throw new Error("저장 형식 오류");
        stored.forEach(validateSession);
        for (const item of stored.filter(hasQuestion)) {
          const key = storagePrefix + item.id;
          if (localStorage.getItem(key) === null)
            localStorage.setItem(key, serialize(item));
        }
        localStorage.removeItem(legacyKey);
      }
    } catch {
      storageError.value =
        "기존 대화 기록을 읽지 못했습니다. 원본 저장 기록을 보존했습니다.";
    }
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (!key?.startsWith(storagePrefix)) continue;
        try {
          refreshSession(key.slice(storagePrefix.length));
        } catch {
          storageError.value =
            "일부 대화 기록을 읽지 못했습니다. 원본 저장 기록을 보존했습니다.";
        }
      }
      // Only the view is recovered; another tab may still own the request.
      sessions.value.forEach(recoverPending);
    } catch {
      storageError.value =
        "기존 대화 기록을 읽지 못했습니다. 원본 저장 기록을 보존했습니다.";
    }
    sessions.value.sort((a, b) =>
      (b.createdAt || "").localeCompare(a.createdAt || ""),
    );
    newSession();
  }
  function syncStorage(event) {
    if (event.storageArea !== localStorage) return;
    const ids =
      event.key === null
        ? [...savedIds]
        : event.key.startsWith(storagePrefix)
          ? [event.key.slice(storagePrefix.length)]
          : [];
    try {
      for (const id of ids) {
        if (unsavedIds.has(id) || (streaming.value && id === currentId.value))
          continue;
        refreshSession(id);
      }
    } catch {
      storageError.value =
        "다른 탭의 대화 기록을 읽지 못했습니다. 원본 저장 기록을 보존했습니다.";
    }
  }

  onMounted(() => {
    loadSessions();
    window.addEventListener("storage", syncStorage);
  });
  onUnmounted(() => window.removeEventListener("storage", syncStorage));
  return {
    sessions,
    history,
    currentId,
    storageError,
    session,
    title,
    groups,
    save,
    editSession,
    newSession,
    selectSession,
    deleteSession,
    clearChat,
    exportChat,
  };
}
