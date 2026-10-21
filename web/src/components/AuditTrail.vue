<script setup>
import { computed, ref } from "vue";
import { formatNumber } from "../client.mjs";
import TraceEvents from "./TraceEvents.vue";
const props = defineProps({ message: { type: Object, required: true } });
const baseline = computed(
  () =>
    props.message.finalAnswer?.route === "baseline" ||
    props.message.finalAnswer?.metadata?.version === "v1",
);
const verified = computed(
  () =>
    !baseline.value &&
    (props.message.events || [])
      .filter((event) => event.type === "Validation")
      .at(-1)?.verdict === "pass",
);
const banner = computed(() =>
  baseline.value
    ? "단순 검색 답변 · 별도 검증 없음"
    : props.message.finalAnswer?.route === "chat"
      ? "대화 응답 · 금융 수치 검증 대상 아님"
      : verified.value
        ? "수치·요청 항목 검사 통과"
        : "답변 · 검증 통과 기록 없음",
);
const open = ref(true);
const progress = computed(() => {
  const last = props.message.events?.at(-1);
  if (last?.type === "AnswerProgress")
    return last.stage === "writing" ? "답변 작성 중…" : "수치 검증 중…";
  if (last?.type === "Validation")
    return last.verdict === "pass" ? "결과 정리 중…" : "답변 재검토 중…";
  if (last?.type === "TaskCompleted") return "조사 결과 수집 중…";
  return "처리 중…";
});
const route = computed(
  () =>
    ({
      easy: "단일 상품 조회",
      hard: "계산·비교 분석",
      baseline: "단순 검색",
      chat: "일반 대화",
      out_of_scope: "답변 범위 확인",
    })[props.message.route] || "질문 분석 중",
);
</script>

<template>
  <section
    class="audit-card"
    aria-label="분석 및 검증 과정"
  >
    <button
      type="button"
      class="trace-header"
      :aria-expanded="open"
      @click="open = !open"
    >
      <span class="audit-title"
        ><span class="material-symbols-outlined">account_tree</span>분석 및 검증
        과정<span class="trace-route-badge">{{ route }}</span></span
      ><span class="audit-state"
        ><span
          class="trace-status-pill"
          role="status"
          >{{
            message.pending
              ? progress
              : message.error
                ? "처리 중단"
                : "응답 완료"
          }}</span
        ><span
          class="trace-chevron material-symbols-outlined"
          :class="{ 'rotate-180': open }"
          >expand_more</span
        ></span
      >
    </button>
    <div
      v-show="open"
      class="trace-body"
    >
      <p class="answer-label">{{ banner }}</p>
      <trace-events :events="message.events"></trace-events>
      <p v-if="!message.pending && !message.events?.length">
        저장된 진행 기록이 없습니다.
      </p>
      <template v-if="message.finalAnswer">
        <div
          v-for="(calc, index) in message.finalAnswer.calculations || []"
          :key="index"
          class="formula-box"
        >
          <div>{{ calc.name }} · 계산식</div>
          <code>{{ calc.formula || "공식 정보 없음" }}</code>
        </div>
        <p
          v-for="(warning, index) in message.finalAnswer.warnings || []"
          :key="index"
          class="data-warning"
        >
          {{ warning }}
        </p>
        <details class="execution-meta">
          <summary>응답 실행 정보</summary>
          <dl>
            <div>
              <dt>처리 시간</dt>
              <dd>
                {{
                  message.finalAnswer.metadata?.latency_ms == null
                    ? "—"
                    : formatNumber(message.finalAnswer.metadata.latency_ms) +
                      "밀리초"
                }}
              </dd>
            </div>
            <div>
              <dt>모델 호출</dt>
              <dd>{{ message.finalAnswer.metadata?.llm_calls ?? "—" }}</dd>
            </div>
            <div>
              <dt>토큰 기준 추정 비용</dt>
              <dd>
                {{
                  message.finalAnswer.metadata?.cost_usd == null
                    ? "—"
                    : "$" +
                      Number(message.finalAnswer.metadata.cost_usd).toFixed(4)
                }}
              </dd>
            </div>
            <div>
              <dt>공시 기준월</dt>
              <dd>{{ message.finalAnswer.metadata?.data_month || "—" }}</dd>
            </div>
          </dl>
          <div
            v-for="run in message.finalAnswer.metadata?.writing_runs || []"
            :key="run.attempt"
            class="writing-timing"
          >
            <p>
              답변 작성 {{ run.attempt }}회차 ·
              {{ formatNumber(run.writing_ms) }}밀리초
            </p>
            <p>
              입력 {{ run.prompt_tokens ?? "—" }} / 출력
              {{ run.completion_tokens ?? "—" }} 토큰
            </p>
            <p v-if="run.gen_ms != null">
              모델 로딩 {{ formatNumber(run.load_ms) }} · 입력 처리
              {{ formatNumber(run.prompt_ms) }} · 생성
              {{ formatNumber(run.gen_ms) }}밀리초
            </p>
          </div>
        </details>
      </template>
    </div>
  </section>
</template>
