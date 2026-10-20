"""답변 검증 (순수 함수): 금융 수치는 근거에 있는 값만, 인용문은 원문에 실제로 있어야 한다."""
from __future__ import annotations

import re

from .catalog import squash

# 단위가 붙은 수치만 검사한다. '1) 2)'나 '3개' 같은 일반 숫자까지 막으면 정상 답도 떨어진다.
FIN_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(%p|%|원|만\s*원|억\s*원|억)")


def financial_values(text: str) -> set[float]:
    out = set()
    for n, unit in FIN_NUM_RE.findall(text):
        v = float(n.replace(",", ""))
        unit = unit.replace(" ", "")
        out.add(round(v * {"만원": 1e4, "억원": 1e8, "억": 1e8}.get(unit, 1), 2))
    return out


def all_numbers(text: str) -> set[float]:
    return {round(float(x.replace(",", "")), 2) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def first_mention(text: str, candidates: list[str]) -> str | None:
    """답에서 가장 먼저 등장하는 후보(같은 위치면 더 긴 이름) — 비교 질문에서 답이 '고른' 대상."""
    t = squash(text)
    found = [(t.find(c), -len(c), c) for c in {squash(x) for x in candidates} if c and c in t]
    return min(found)[2] if found else None


def check_answer(reply: str, evidence: str, citations: list[dict], sections: dict[str, str],
                 required: list[float] = (), winner: tuple[list[str], list[str]] | None = None
                 ) -> tuple[bool, str | None]:
    """evidence: 도구 결과·검색 문서·질문을 이어 붙인 문자열. sections: product_id -> 원문."""
    allowed = all_numbers(evidence) | financial_values(evidence)
    bad = sorted(v for v in financial_values(reply) if v not in allowed)
    if bad:
        return False, f"근거에 없는 수치 {bad}"
    for c in citations:
        src = sections.get(c.get("product_id", "").strip(" []"))
        if src is None:
            return False, f"없는 상품을 인용함: {c.get('product_id')}"
        if "|" in c.get("quote", ""):
            return False, "금리표는 인용하지 말고 '상품 설명' 아래 문장만 그대로 인용하세요(표의 숫자는 답에 그대로 쓰면 됩니다)"
        if squash(c.get("quote", "")) not in squash(src):
            return False, f"원문에 없는 인용: {c.get('quote', '')[:40]}"
    if winner:  # 1위를 묻는 질문: 답이 처음 언급한 상품·회사가 조회 1위여야 한다
        tops, candidates = winner
        picked = first_mention(reply, candidates)
        if picked not in {squash(x) for x in tops}:
            return False, f"조회 1위는 {', '.join(tops)}입니다. 1위부터 답하세요"
    missing = [v for v in required if round(float(v), 2) not in financial_values(reply)]
    if missing:
        return False, f"계산 결과 {missing}가 답에 없음"
    return True, None
