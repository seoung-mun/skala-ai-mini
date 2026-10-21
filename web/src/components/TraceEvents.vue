<script setup>
defineProps({ events: { type: Array, default: () => [] } });
const agent = (value) =>
  ({ structured: "상품 조회", rag: "공시 검색", calc: "계산" })[value] ||
  "분석";
const checks = (event) =>
  event.type === "Validation" || event.type === "ReplanCheck";
const passed = (event) =>
  event.type === "Validation" ? event.verdict === "pass" : event.is_sufficient;
function title(event) {
  if (event.type === "RouteDecided") return "질문 유형 및 조회 경로 확인";
  if (event.type === "PlanCreated") return "조회 및 계산 계획 수립";
  if (event.type === "AnswerProgress")
    return event.stage === "writing"
      ? `답변 ${event.attempt > 1 ? "다시 " : ""}작성 시작 (${event.attempt}차)`
      : `답변 작성 완료 · 수치 검증 시작 (${event.attempt}차)`;
  if (event.type === "TaskCompleted")
    return `${agent(event.agent)} 작업 ${event.ok ? "완료" : "실패"}`;
  return event.type === "Validation"
    ? `검증 ${event.attempt}차: ${passed(event) ? "통과" : "불일치"}`
    : "자료 확인";
}
function detail(event) {
  if (event.type === "AnswerProgress")
    return event.stage === "writing"
      ? "모은 자료로 LLM이 답변을 작성합니다. 검증 후 표시됩니다."
      : `답변 작성 ${(event.writing_ms / 1000).toFixed(1)}초 · 금액·금리·순위를 확인합니다.`;
  if (event.type === "PlanCreated")
    return (event.tasks || [])
      .map((task) => `${agent(task.agent)}: ${task.desc}`)
      .join(" · ");
  if (event.type === "TaskCompleted")
    return `작업 ${event.id} · ${event.elapsed_ms ?? "—"}밀리초 · 재시도 ${event.retries ?? "—"}회`;
  return event.reason || "";
}
</script>

<template>
  <div class="trace-steps-container">
    <template
      v-for="(event, index) in events"
      :key="index"
    >
      <span
        v-if="checks(event)"
        class="audit-check"
        :class="passed(event) ? 'passed' : 'failed'"
        >{{ title(event)
        }}<template v-if="detail(event)"> · {{ detail(event) }}</template></span
      >
      <div
        v-else-if="
          [
            'RouteDecided',
            'PlanCreated',
            'TaskCompleted',
            'AnswerProgress',
          ].includes(event.type)
        "
        class="audit-step"
        :class="{ failed: event.ok === false }"
      >
        <span class="material-symbols-outlined">{{
          event.type === "AnswerProgress"
            ? "edit_note"
            : event.ok === false
              ? "error"
              : "check_circle"
        }}</span>
        <div>
          <p>{{ title(event) }}</p>
          <small>{{ detail(event) }}</small>
        </div>
      </div>
    </template>
  </div>
</template>
