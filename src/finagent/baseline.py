"""v1 베이스라인: 라우팅·계획·검증·계산 도구 없이 [키워드 검색 top-k -> LLM 1회]로 모든 질문을 처리한다."""
from __future__ import annotations

import time

from langsmith import traceable

from .search import KeywordIndex

SYSTEM = ("당신은 한국 금융상품 상담원이다. 아래 '참고 문서'에 있는 내용만 근거로 한국어로 간결하게 답하라. "
          "금리·금액 같은 수치는 문서 값을 그대로 인용하고, 계산이 필요하면 직접 계산하라. "
          "문서에 없는 내용은 모른다고 답하라.")


@traceable(name="baseline_v1")
def run_baseline(query: str, index: KeywordIndex, llm, k: int = 5) -> dict:
    started = time.perf_counter()
    hits = index.search(query, k)
    docs = "\n\n---\n\n".join(index.docs[i] for i, _ in hits) or "(검색 결과 없음)"
    answer, usage = llm.chat(SYSTEM, f"## 참고 문서\n{docs}\n\n## 질문\n{query}")
    return {"answer": answer, "retrieved": [i for i, _ in hits], "usage": usage,
            "llm_calls": 1, "retries": 0, "latency_s": round(time.perf_counter() - started, 2)}
