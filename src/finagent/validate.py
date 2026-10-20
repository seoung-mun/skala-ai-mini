"""답변 검증 (순수 함수): 금융 수치는 근거에 있는 값만, 인용문은 원문에 실제로 있어야 한다."""
from __future__ import annotations

import re

from .catalog import squash

# 단위가 붙은 수치만 검사한다. '1) 2)'나 '3개' 같은 일반 숫자까지 막으면 정상 답도 떨어진다.
FIN_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(%p|%|원|만\s*원|억\s*원|억)")


def unit_values(text: str) -> list[tuple[int, str, float]]:
    """[(위치, 'pct'|'won', 값)] — 등장 순서대로. 만원·억은 원으로 환산한다."""
    out = []
    for m in FIN_NUM_RE.finditer(text):
        v, unit = float(m.group(1).replace(",", "")), m.group(2).replace(" ", "")
        kind = "pct" if unit.startswith("%") else "won"
        out.append((m.start(), kind, round(v * {"만원": 1e4, "억원": 1e8, "억": 1e8}.get(unit, 1), 2)))
    return out


def financial_values(text: str) -> set[float]:
    return {v for _, _, v in unit_values(text)}


def values_of(text: str, kind: str) -> set[float]:
    return {v for _, k, v in unit_values(text) if k == kind}


def allowed_values(inp, query: str) -> dict[str, set[float]]:
    """답에 쓸 수 있는 수치를 단위별로 모은다. 기간(12개월)이 금리(12%)를 통과시키지 않도록 단위를 섞지 않는다.
    inp는 schemas.AnswerInput."""
    from .slots import won_amounts
    pct = {round(r.value, 2) for r in inp.rows}
    won: set[float] = set()
    for c in inp.calculations:
        won |= {float(c.result)} | ({float(c.after_tax)} if c.after_tax is not None else set())
        if c.inputs.get("amount_won") is not None:
            won.add(float(c.inputs["amount_won"]))
        if c.inputs.get("rate_pct") is not None:
            pct.add(round(float(c.inputs["rate_pct"]), 2))
    for d in inp.documents:
        pct |= {round(float(x), 2) for x in re.findall(r"\d+\.\d+", d.rate_table)}  # 표의 금리는 소수로 렌더링된다
        text = " ".join([*(x.text for x in d.descriptions), *(str(v) for v in d.info.values())])
        pct |= values_of(text, "pct")
        won |= values_of(text, "won")
    pct |= values_of(query, "pct")
    won |= values_of(query, "won") | {float(v) for v in won_amounts(query)}
    return {"pct": pct, "won": won}


def first_mention(text: str, candidates: list[str]) -> str | None:
    """답에서 가장 먼저 등장하는 후보(같은 위치면 더 긴 이름) — 비교 질문에서 답이 '고른' 대상.
    후보에는 상품을 특정할 수 있는 이름만 넣는다('정기예금' 같은 일반명은 질문을 되풀이한 답에서 오판된다)."""
    t = squash(text)
    found = [(t.find(c), -len(c), c) for c in {squash(x) for x in candidates} if c and c in t]
    return min(found)[2] if found else None


def split_citations(citations: list[dict], sections: dict[str, str]) -> tuple[list[dict], list[str]]:
    """원문과 일치하는 인용만 남긴다. 버린 인용은 이유 목록으로 돌려줘 경고로 드러낸다."""
    kept, dropped = [], []
    for c in citations:
        pid, quote = c.get("product_id", "").strip(" []"), c.get("quote", "")
        if "|" in quote:
            dropped.append(f"금리표 인용: {quote[:30]}")
        elif pid not in sections or not quote.strip() or squash(quote) not in squash(sections[pid]):
            dropped.append(f"원문 불일치: {quote[:30]}")
        else:
            kept.append({**c, "product_id": pid})
    return kept, dropped


def check_answer(reply: str, allowed: dict[str, set[float]], citations: list[dict], sections: dict[str, str],
                 required: list[tuple[float, ...]] = (), winner: tuple[list[str], list[str]] | None = None
                 ) -> tuple[bool, str | None]:
    """allowed: allowed_values()의 단위별 허용 수치. sections: product_id -> 인용 가능한 원문."""
    bad = sorted({f"{v:g}{'%' if k == 'pct' else '원'}" for _, k, v in unit_values(reply) if v not in allowed[k]})
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
    got = financial_values(reply)
    missing = [g for g in required if not any(round(float(v), 2) in got for v in g)]  # 세전·세후 중 하나면 충분
    if missing:
        return False, f"계산 결과 {[int(g[0]) for g in missing]}원(또는 세후 금액)이 답에 없음"
    return True, None
