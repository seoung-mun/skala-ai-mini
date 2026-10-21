<script setup>
import { ref, computed, nextTick, watch, onMounted, onUnmounted } from "vue";
import AppHeader from "./components/AppHeader.vue";
import SessionSidebar from "./components/SessionSidebar.vue";
import AuditTrail from "./components/AuditTrail.vue";
import FinalAnswer from "./components/FinalAnswer.vue";
import { scrollMessages, getSessionRecords } from "./client.mjs";
import { useSessions } from "./composables/useSessions.mjs";
import { useChatStream } from "./composables/useChatStream.mjs";
import { useServiceStatus } from "./composables/useServiceStatus.mjs";

const view = ref("chat");
const sidebarOpen = ref(false);
const streaming = ref(false);
const canvas = ref(null);
const recordsPanel = ref(null);
const chatInput = ref(null);
const previewValue = new URLSearchParams(location.search).get("preview");
const preview = ["easy", "hard", "error", "v1"].includes(previewValue)
  ? previewValue
  : null;
const sessionState = useSessions(streaming, preview);
const {
  history: recordedSessions,
  currentId,
  storageError,
  session,
  title,
  groups,
  clearChat,
  exportChat,
  save,
} = sessionState;

const { input, send, retry, stop } = useChatStream(
  session,
  save,
  streaming,
  preview,
);
const { health, clock, month, status, checkHealth } = useServiceStatus();
const records = computed(() =>
  getSessionRecords(session.value).filter((record) =>
    view.value === "traces"
      ? record.events.length || record.error || record.message.finalAnswer
      : record.citations.length || record.products.length,
  ),
);
const prompts = [
  {
    label: "✨ 우대조건 확인하기",
    text: "우리SUPER주거래적금 우대조건 알려줘",
  },
  {
    label: "📊 타 은행 금리 비교",
    text: "신한은행 정기예금 vs 우리은행 정기예금 금리 비교해줘",
  },
  {
    label: "🧾 세후 실수령액 보기",
    text: "우리SUPER주거래적금 월 50만원씩 12개월 세후 이자 얼마야?",
  },
];
async function scrollLatest() {
  await nextTick();
  scrollMessages(canvas.value);
}
async function setView(value) {
  view.value = value;
  await nextTick();
  if (value !== "chat" && recordsPanel.value) {
    recordsPanel.value.scrollTop = 0;
    recordsPanel.value.focus();
  }
}

function newSession(persist = true) {
  if (streaming.value) return;
  sessionState.newSession(persist);
  view.value = "chat";
  sidebarOpen.value = false;
  nextTick(() => chatInput.value?.focus());
}
function selectSession(id) {
  if (streaming.value) return;
  sessionState.selectSession(id);
  sidebarOpen.value = false;
  scrollLatest();
}
function deleteSession(id) {
  if (streaming.value) return;
  const wasLast = recordedSessions.value.length === 1;
  sessionState.deleteSession(id);
  if (wasLast) {
    view.value = "chat";
    sidebarOpen.value = false;
    nextTick(() => chatInput.value?.focus());
  }
  scrollLatest();
}
function goHome() {
  setView("chat");
  chatInput.value?.focus();
}
function keyboard(event) {
  if (event.key === "Escape") sidebarOpen.value = false;
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    newSession();
  }
}

watch(streaming, (active) => {
  if (active) {
    view.value = "chat";
    scrollLatest();
  }
});
onMounted(() => {
  scrollLatest();
  if (!session.value?.messages.length) nextTick(() => chatInput.value?.focus());
  window.addEventListener("keydown", keyboard);
});
onUnmounted(() => window.removeEventListener("keydown", keyboard));
</script>

<template>
  <AppHeader
    :view="view"
    :sidebar-open="sidebarOpen"
    :health="health"
    :status="status"
    :month="month"
    :streaming="streaming"
    @toggle-sidebar="sidebarOpen = !sidebarOpen"
    @home="goHome"
    @change-view="setView"
    @refresh="checkHealth"
    @export="exportChat"
    @clear="clearChat"
  />
  <div class="workspace">
    <SessionSidebar
      :sessions="recordedSessions"
      :current-id="currentId"
      :groups="groups"
      :sidebar-open="sidebarOpen"
      :health="health"
      :status="status"
      :month="month"
      :clock="clock"
      :streaming="streaming"
      @new-session="newSession()"
      @select-session="selectSession"
      @delete-session="deleteSession"
    />
    <div
      v-show="sidebarOpen"
      class="sidebar-backdrop"
      @click="sidebarOpen = false"
    ></div>
    <main>
      <div class="session-bar">
        <div class="session-context">
          <span class="session-icon material-symbols-outlined">calculate</span>
          <div>
            <h1 id="session-title">{{ title }}</h1>
            <p>
              대화 번호: <span>{{ currentId }}</span> · 공시 기반 금융 상담
            </p>
          </div>
        </div>
        <div class="session-status">
          <span v-if="streaming">분석 중…</span
          ><span class="verification-mode"
            ><span class="material-symbols-outlined">verified</span>공시 기반
            답변</span
          >
        </div>
      </div>
      <div
        v-if="storageError"
        class="data-warning"
        role="alert"
      >
        {{ storageError }}
      </div>
      <div
        id="chat-messages-container"
        ref="canvas"
        v-show="view === 'chat'"
        class="message-canvas custom-scrollbar"
        tabindex="-1"
        aria-label="대화 내용"
      >
        <div
          v-if="!session?.messages.length"
          class="welcome"
        >
          <span class="welcome-icon material-symbols-outlined"
            >account_balance</span
          >
          <h2>금융상품, 근거를 보고 선택하세요.</h2>
          <p>
            금리와 가입 조건을 확인하고, 예상 이자를 계산해 보세요.<br />답변에
            사용한 공시와 계산 과정을 함께 보여드립니다.
          </p>
          <button
            class="prompt-chip welcome-prompt"
            :disabled="streaming"
            @click="
              send(
                '우리SUPER주거래적금 월 50만원씩 12개월 부으면 이자 얼마 나와?',
              )
            "
          >
            <span class="material-symbols-outlined">calculate</span>월 50만원,
            12개월 적금 이자 알아보기<span class="material-symbols-outlined"
              >arrow_forward</span
            >
          </button>
        </div>
        <template
          v-for="(message, index) in session?.messages || []"
          :key="currentId + '-' + index"
        >
          <div
            v-if="message.role === 'user'"
            class="chat-bubble-row user-row"
          >
            <div class="user-bubble">
              <p>{{ message.content }}</p>
              <div class="user-time">
                <span class="material-symbols-outlined">schedule</span
                >{{ message.timestamp }}
              </div>
            </div>
            <span class="user-avatar">나</span>
          </div>
          <div
            v-else-if="message.role === 'assistant'"
            class="chat-bubble-row assistant-row"
          >
            <span class="assistant-avatar material-symbols-outlined"
              >smart_toy</span
            >
            <div class="assistant-content">
              <div
                v-if="message.source === 'mock'"
                class="mock-notice"
              >
                개발용 계약 예시 · 실제 조회 결과가 아닙니다.
              </div>
              <p
                v-if="message.pending && !message.finalAnswer"
                class="answer-pending"
                role="status"
              >
                답변 준비 중…
                <button
                  type="button"
                  class="prompt-chip"
                  @click="setView('traces')"
                >
                  진행 보기
                </button>
              </p>
              <div
                class="final-answer-slot"
                aria-live="polite"
              >
                <div
                  v-if="message.error"
                  class="error-card"
                  role="alert"
                >
                  <h3>오류 발생 ({{ message.error.code || "ERROR" }})</h3>
                  <p>{{ message.error.message }}</p>
                  <button
                    class="prompt-chip btn-retry"
                    :disabled="streaming"
                    @click="retry(index)"
                  >
                    다시 요청하기
                  </button>
                </div>
                <final-answer
                  v-else-if="message.finalAnswer"
                  :answer="message.finalAnswer"
                  :events="message.events"
                  :animate="message.revealAnswer === true"
                  @revealed="message.revealAnswer = false"
                ></final-answer>
              </div>
            </div>
          </div>
        </template>
      </div>
      <section
        id="session-records"
        ref="recordsPanel"
        v-show="view !== 'chat'"
        class="message-canvas records-panel"
        tabindex="-1"
        aria-label="대화 기록 상세"
      >
        <header class="records-heading">
          <h2>{{ view === "traces" ? "분석 과정" : "답변의 출처" }}</h2>
          <p>
            {{
              view === "traces"
                ? "이 대화에서 수행한 조회·계산·검증 기록입니다."
                : "이 대화의 답변에 사용된 공시 원문입니다."
            }}
          </p>
        </header>
        <div
          v-if="!records.length"
          class="records-empty"
        >
          <span class="material-symbols-outlined">{{
            view === "traces" ? "account_tree" : "format_quote"
          }}</span>
          <h3>
            {{
              view === "traces"
                ? "아직 분석 기록이 없습니다."
                : "아직 제공된 출처가 없습니다."
            }}
          </h3>
          <p>
            {{
              view === "traces"
                ? "질문을 보내면 서버가 전달하는 진행 과정이 여기에 표시됩니다."
                : "답변에 원문 인용이 포함되면 여기에 표시됩니다."
            }}
          </p>
        </div>
        <article
          v-for="(record, index) in records"
          :key="index"
          class="record-card"
        >
          <h3>{{ record.question }}</h3>
          <p
            v-if="record.source === 'mock'"
            class="mock-notice"
          >
            개발용 계약 예시 기록
          </p>
          <template v-if="view === 'traces'"
            ><audit-trail :message="record.message"></audit-trail>
            <p
              v-if="record.error"
              class="data-warning"
            >
              {{ record.error.message }}
            </p></template
          ><template v-else
            ><div
              v-for="product in record.products"
              :key="product.id"
              class="source-product"
            >
              <strong>{{ product.bank }} · {{ product.product }}</strong>
              <small
                >공시 스냅샷
                {{ record.metadata?.data_month || "기준월 미상" }} · 상품 코드
                {{ product.id }}</small
              >
              <p
                v-if="product.data_flags?.length"
                class="data-warning"
              >
                {{ product.data_flags.join(" · ") }}
              </p>
            </div>
            <p
              v-if="!record.citations.length"
              class="data-warning"
            >
              저장된 원문 인용이 없어 조회 상품 정보만 표시합니다.
            </p>
            <blockquote
              v-for="(citation, index) in record.citations"
              :key="index"
            >
              <strong
                >{{
                  record.products.find((p) => p.id === citation.product_id)
                    ?.product || "공시 상품"
                }}
                · {{ citation.section || "공시 원문" }}</strong
              >
              <p>{{ citation.quote }}</p>
              <small>상품 코드: {{ citation.product_id || "—" }}</small>
            </blockquote></template
          >
        </article>
      </section>
      <footer class="composer">
        <div class="composer-inner">
          <div
            v-if="preview"
            class="mock-controls"
            role="status"
          >
            개발용 계약 예시 재생 중입니다. 실제 조회 결과가 아닙니다.
            <a href="/">실제 상담으로 돌아가기</a>
          </div>
          <div class="suggestions scrollbar-none">
            <span class="suggestions-label"
              ><span class="material-symbols-outlined">auto_awesome</span>추천
              질의:</span
            ><button
              v-for="prompt in prompts"
              :key="prompt.text"
              class="prompt-chip"
              :disabled="streaming"
              @click="send(prompt.text)"
            >
              {{ prompt.label }}
            </button>
          </div>
          <form
            id="chat-form"
            class="input-box"
            @submit.prevent="send()"
          >
            <span
              class="material-symbols-outlined input-icon"
              aria-hidden="true"
              >chat_bubble_outline</span
            ><input
              id="chat-input"
              ref="chatInput"
              v-model="input"
              aria-label="금융상품 질문"
              placeholder="금융상품의 금리, 조건, 예상 이자를 질문하세요..."
              type="text"
              maxlength="500"
              autocomplete="off"
            /><span
              id="char-counter"
              :class="{ 'text-error': input.length >= 480 }"
              >{{ input.length }} / 500</span
            ><button
              v-if="streaming"
              id="btn-stop-stream"
              type="button"
              class="stop-button"
              @click="stop"
            >
              <span class="material-symbols-outlined">stop</span>중단</button
            ><button
              v-else
              id="btn-send"
              type="submit"
              class="send-button"
              :disabled="!input.trim()"
            >
              전송<span class="material-symbols-outlined">arrow_upward</span>
            </button>
          </form>
          <div class="composer-footnote">
            <span>FinAgent는 금융감독원 공시 데이터를 기반으로 답변합니다.</span
            ><span>공시 기준: {{ month }}</span>
          </div>
        </div>
      </footer>
    </main>
  </div>
</template>
