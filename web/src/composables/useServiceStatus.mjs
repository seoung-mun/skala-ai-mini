import { ref, reactive, computed, onMounted, onUnmounted } from "vue";
import { formatTime } from "../client.mjs";

export function useServiceStatus() {
  const clock = ref(formatTime());
  const health = reactive({
    checked: false,
    online: false,
    month: null,
    llm: null,
  });
  const month = computed(() =>
    health.month
      ? health.month.replace(/^(\d{4})(\d{2})$/, "$1.$2")
      : "확인 중",
  );
  const status = computed(() =>
    !health.checked
      ? "연결 확인 중"
      : health.online
        ? "공시 데이터 연결됨"
        : "백엔드 연결 안 됨",
  );
  let healthController, clockTimer, healthTimer;
  async function checkHealth() {
    healthController?.abort();
    const request = (healthController = new AbortController());
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(3000)]);
    try {
      const response = await fetch("/health", { signal });
      const data = await response.json();
      if (!response.ok || data.status !== "ok") throw new Error("연결 실패");
      Object.assign(health, {
        checked: true,
        online: true,
        month: data.data_month || null,
        llm: data.llm || null,
      });
    } catch {
      if (request.signal.aborted) return;
      Object.assign(health, { checked: true, online: false, llm: null });
    }
  }

  onMounted(() => {
    checkHealth();
    clockTimer = setInterval(() => {
      clock.value = formatTime();
    }, 1000);
    healthTimer = setInterval(checkHealth, 8000);
  });
  onUnmounted(() => {
    healthController?.abort();
    clearInterval(clockTimer);
    clearInterval(healthTimer);
  });
  return { health, clock, month, status, checkHealth };
}
