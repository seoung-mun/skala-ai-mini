<script setup>
import { computed, watch, onUnmounted } from "vue";
import { useProgressiveText } from "../composables/useProgressiveText.mjs";
import {
  formatAnswerText,
  formatNumber,
  renderSimpleMarkdown,
} from "../client.mjs";
const props = defineProps({
  answer: { type: Object, required: true },
  events: { type: Array, default: () => [] },
  animate: { type: Boolean, default: false },
});
const emit = defineEmits(["revealed"]);
const displayReply = computed(() =>
  formatAnswerText(props.answer.reply, props.answer.products),
);
const { visible, revealing, finish } = useProgressiveText(
  displayReply,
  computed(() => props.animate),
);
watch(revealing, (active, previous) => {
  if (previous && !active) emit("revealed");
});
onUnmounted(() => emit("revealed"));
const resultLabel = (calc) =>
  calc.inputs?.mode === "goal"
    ? calc.name || "역산 결과"
    : ["loan", "mortgage", "rent", "credit"].includes(calc.inputs?.product_type)
      ? "월 상환액"
      : "세전 이자";
</script>

<template>
  <div>
    <section class="answer-card">
      <div
        class="prose-finagent answer-text"
        :aria-hidden="revealing ? 'true' : undefined"
        :class="{ 'answer-revealing': revealing }"
        v-html="renderSimpleMarkdown(visible)"
      ></div>
      <template v-if="revealing">
        <span class="sr-only">{{ displayReply }}</span>
        <button
          type="button"
          class="reveal-skip"
          @click="finish"
        >
          전체 답변 바로 보기
        </button>
      </template>
    </section>
    <section
      v-for="(calc, index) in answer.calculations || []"
      :key="index"
      class="calculation-card"
    >
      <div class="card-heading">
        <h2>
          <span class="material-symbols-outlined">table_chart</span>산출 상세
          명세
        </h2>
        <span>{{ calc.name }}</span>
      </div>
      <div class="input-metrics">
        <div class="metric">
          <span>{{
            calc.inputs?.mode === "goal"
              ? calc.inputs.product_type === "loan"
                ? "월 상환 가능액"
                : "목표 세후 만기 수령액"
              : calc.inputs?.product_type === "saving" ||
                  calc.inputs?.monthly_won != null
                ? "월 납입금"
                : "계산 금액"
          }}</span
          ><strong
            >{{
              calc.inputs?.amount_won == null &&
              calc.inputs?.monthly_won == null
                ? "—"
                : formatNumber(
                    calc.inputs.amount_won ?? calc.inputs.monthly_won,
                  )
            }}<small>원</small></strong
          >
        </div>
        <div class="metric">
          <span>납입 기간</span
          ><strong
            >{{ calc.inputs?.term_months ?? "—" }}<small>개월</small></strong
          >
        </div>
        <div class="metric">
          <span>적용 금리</span
          ><strong class="purple-text"
            >{{
              calc.inputs?.rate_pct == null
                ? "—"
                : "연 " + formatNumber(calc.inputs.rate_pct)
            }}<small
              >%{{
                calc.inputs?.interest_type
                  ? " · " +
                    ({ simple: "단리", compound: "복리" }[
                      calc.inputs.interest_type
                    ] || calc.inputs.interest_type)
                  : ""
              }}</small
            ></strong
          >
        </div>
      </div>
      <div class="output-metrics">
        <div class="result-metric">
          <span>{{ resultLabel(calc) }}</span
          ><strong
            >{{ calc.result == null ? "—" : formatNumber(calc.result)
            }}<small>{{ calc.unit }}</small></strong
          >
        </div>
        <div
          v-if="calc.after_tax != null"
          class="result-metric secondary-result"
        >
          <span>세후 이자</span
          ><strong
            >{{ formatNumber(calc.after_tax)
            }}<small>{{ calc.unit }}</small></strong
          >
        </div>
      </div>
    </section>
    <section
      v-for="(product, index) in answer.products || []"
      :key="product.id || index"
      class="product-card"
    >
      <div class="product-heading">
        <span class="bank-icon">{{ (product.bank || "").slice(0, 2) }}</span>
        <div>
          <div class="product-kind">
            <span class="small-badge">{{
              product.kind_ko || product.kind || "금융상품"
            }}</span
            >{{ product.bank }}
          </div>
          <h2>{{ product.product }}</h2>
        </div>
      </div>
      <div class="product-details">
        <div>
          <span>{{ product.highlight?.label || "공시 금리" }}</span
          ><strong class="purple-text">{{
            product.highlight?.value == null
              ? "—"
              : String(product.highlight.value) + (product.highlight.unit || "")
          }}</strong>
        </div>
        <div>
          <span>금융권 구분</span
          ><strong>{{
            { bank: "은행", savingsbank: "저축은행" }[product.group] || "—"
          }}</strong>
        </div>
        <div>
          <span>공시 상태</span><strong>{{ product.sale || "—" }}</strong>
        </div>
      </div>
    </section>
  </div>
</template>
