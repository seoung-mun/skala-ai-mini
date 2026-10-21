"""답변 검증 (순수 함수): 금융 수치는 근거에 있는 값만, 인용문은 원문에 실제로 있어야 한다."""

from __future__ import annotations

import re

from .amounts import NUMBER, money_values
from .catalog import squash

# 단위가 붙은 수치만 검사한다. '1) 2)'나 '3개' 같은 일반 숫자까지 막으면 정상 답도 떨어진다.
FIN_NUM_RE = re.compile(
    rf"(?<![\d.,])[+−-]?({NUMBER})\s*(%p|퍼센트\s*포인트|퍼센트|%)",
    re.IGNORECASE,
)


def unit_values(text: str) -> list[tuple[int, str, float]]:
    """금액은 복합 단위 전체, 금리는 기호·한글 표기를 원문 순서대로 읽는다."""
    out = [(pos, "won", float(value)) for pos, value in money_values(text)]
    for match in FIN_NUM_RE.finditer(text):
        number = float(match[1].replace(",", ""))
        if match[0].startswith(("-", "−")):
            number = -number
        out.append((match.start(), "pct", number))
    return sorted(out)


def financial_values(text: str) -> set[float]:
    """텍스트의 단위 붙은 금융 수치 집합(단위 구분 없이)."""
    return {v for _, _, v in unit_values(text)}


def values_of(text: str, kind: str) -> set[float]:
    """텍스트에서 지정 단위('pct' 또는 'won')의 수치 집합."""
    return {v for _, k, v in unit_values(text) if k == kind}


def allowed_values(inp, query: str) -> dict[str, set[float]]:
    """답에 쓸 수 있는 수치를 단위별로 모은다. 기간(12개월)이 금리(12%)를 통과시키지 않도록 단위를 섞지 않는다.
    inp는 schemas.AnswerInput."""
    from .slots import won_amounts

    pct = {round(r.value, 2) for r in inp.rows}
    won: set[float] = set()
    for c in inp.calculations:
        won |= {float(c.result)} | (
            {float(c.after_tax)} if c.after_tax is not None else set()
        )
        if c.inputs.get("amount_won") is not None:
            won.add(float(c.inputs["amount_won"]))
        if c.inputs.get("rate_pct") is not None:
            pct.add(round(float(c.inputs["rate_pct"]), 2))
    for d in inp.documents:
        pct |= {
            round(float(x), 2) for x in re.findall(r"\d+\.\d+", d.rate_table)
        }  # 표의 금리는 소수로 렌더링된다
        text = " ".join(
            [
                *(x.text for x in d.descriptions),
                *(str(v) for v in d.info.values()),
            ]
        )
        pct |= values_of(text, "pct")
        won |= values_of(text, "won")
    pct |= values_of(query, "pct")
    won |= values_of(query, "won") | {float(v) for v in won_amounts(query)}
    return {"pct": pct, "won": won}


def first_mention(text: str, candidates: list[str]) -> str | None:
    """답에서 가장 먼저 등장하는 후보(같은 위치면 더 긴 이름) — 비교 질문에서 답이 '고른' 대상.
    후보에는 상품을 특정할 수 있는 이름만 넣는다('정기예금' 같은 일반명은 질문을 되풀이한 답에서 오판된다)."""
    t = squash(text)
    found = [
        (t.find(c), -len(c), c)
        for c in {squash(x) for x in candidates}
        if c and c in t
    ]
    return min(found)[2] if found else None


def split_citations(
    citations: list[dict],
    sections: dict[str, str],
    section_sources: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """원문과 일치하는 인용만 남긴다. 버린 인용은 이유 목록으로 돌려줘 경고로 드러낸다."""
    kept, dropped = [], []
    for c in citations:
        pid, quote = c.get("product_id", "").strip(" []"), c.get("quote", "")
        if "|" in quote:
            dropped.append(f"금리표 인용: {quote[:30]}")
        elif (
            pid not in sections
            or not quote.strip()
            or squash(quote) not in squash(sections[pid])
        ):
            dropped.append(f"원문 불일치: {quote[:30]}")
        elif section_sources is not None and (
            c.get("section") not in section_sources.get(pid, {})
            or squash(quote) not in squash(section_sources[pid][c["section"]])
        ):
            dropped.append(f"인용 섹션 불일치: {c.get('section', '')}")
        else:
            kept.append({**c, "product_id": pid})
    return kept, dropped


def condition_rate(query: str, documents) -> tuple[str, set[float]] | None:
    """개별 조건을 묻는 경우 공시의 해당 줄에 금리가 하나일 때만 답의 속성을 확정한다."""
    if re.search(r"기본\s*금리|최고\s*금리|우대\s*(금리\s*)?폭", query):
        return None
    if not re.search(r"우대|더\s*붙|얼마나\s*더|추가", query):
        return None
    conditions = (
        (
            r"월급|급여|소득이체|연금",
            r"급여|소득이체|연금",
            "급여·연금 이체 우대",
        ),
        (r"신규\s*고객|첫\s*거래", r"신규\s*고객|첫\s*거래", "신규고객 우대"),
    )
    matching = [
        (source, label)
        for cue, source, label in conditions
        if re.search(cue, query)
    ]
    if len(matching) != 1:
        return None
    for source, label in matching:
        vals = set()
        for doc in documents:
            for desc in doc.descriptions:
                if desc.section != "우대조건":
                    continue
                for line in desc.text.splitlines():
                    if re.search(source, line):
                        vals |= values_of(line, "pct")
        if len(vals) == 1:
            return label, vals
    return None


def check_answer(
    reply: str,
    allowed: dict[str, set[float]],
    citations: list[dict],
    sections: dict[str, str],
    required: list[tuple[float, ...]] = (),
    winner: tuple[list[str], list[str]] | None = None,
    attribute: tuple[str, set[float]] | None = None,
) -> tuple[bool, str | None]:
    """allowed: allowed_values()의 단위별 허용 수치. sections: product_id -> 인용 가능한 원문.
    attribute: (질문이 묻는 값의 이름, 허용값) — 답의 첫 금리 수치가 그 값이어야 한다.
    반려 사유는 첫 실패에서 멈추지 않고 모두 모은다: 하나씩 고치다 재작성 한도에 걸리지 않게."""
    why: list[str] = []
    bad = sorted(
        {
            f"{v:g}{'%' if k == 'pct' else '원'}"
            for _, k, v in unit_values(reply)
            if v not in allowed[k]
        }
    )
    if bad:
        why.append(
            f"근거에 없는 수치 {bad}. 조회·계산 결과와 문서에 있는 숫자만 그대로 쓰세요(반올림·환산 금지)"
        )
    # 직접 계산 금지 정책: 자연어 합산은 숫자가 각각 근거에 있어도 검증된 계산식이 아니다.
    symbolic = re.search(
        r"\d[\d,.]*\s*%p?\s*[-+−＋－×÷*/]\s*\d|"
        r"\d[\d,.]*\s*[-+−＋－×÷*/]\s*\d[\d,.]*\s*[=＝]",
        reply,
    )
    verbal = re.search(
        r"더해|더하|더한|합하면|합산|합쳐|합친|차감|빼면|뺀", reply
    )
    if symbolic or (
        verbal and len([v for _, k, v in unit_values(reply) if k == "pct"]) >= 2
    ):
        why.append(
            "검증되지 않은 금리 합산·차감 설명입니다. 직접 계산하지 말고 "
            "질문한 공시 금리만 답하세요"
        )
    for c in citations:
        src = sections.get(c.get("product_id", "").strip(" []"))
        if src is None:
            why.append(f"없는 상품을 인용함: {c.get('product_id')}")
        elif "|" in c.get("quote", ""):
            why.append(
                "금리표는 인용하지 말고 '상품 설명' 아래 문장만 그대로 "
                "인용하세요(표의 숫자는 답에 그대로 쓰면 됩니다)"
            )
        elif squash(c.get("quote", "")) not in squash(src):
            why.append(f"원문에 없는 인용: {c.get('quote', '')[:40]}")
    if attribute:
        name, vals = attribute
        first = next((v for _, k, v in unit_values(reply) if k == "pct"), None)
        if first not in vals:
            unit = "%p" if "우대" in name and "포함" not in name else "%"
            why.append(
                f"질문은 {name}을 묻습니다. 첫 금리 수치로 {', '.join(f'{v:g}{unit}' for v in sorted(vals))}를 쓰세요"
            )
    if (
        winner
    ):  # 1위를 묻는 질문: 답이 처음 언급한 상품·회사가 조회 1위여야 한다
        tops, candidates = winner
        picked = first_mention(reply, candidates)
        if picked not in {squash(x) for x in tops}:
            why.append(f"조회 1위는 {', '.join(tops)}입니다. 1위부터 답하세요")
    got = financial_values(reply)
    missing = [
        g for g in required if not any(round(float(v), 2) in got for v in g)
    ]  # 세전·세후 중 하나면 충분
    if missing:
        why.append(
            f"계산 결과 {[int(g[0]) for g in missing]}원(또는 세후 금액)이 답에 없음"
        )
    if not why:
        return True, None
    return False, why[0] if len(why) == 1 else "\n".join(
        f"{i}. {w}" for i, w in enumerate(why, 1)
    )
