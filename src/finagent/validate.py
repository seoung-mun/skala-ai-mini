"""답변 검증 (순수 함수): 금융 수치는 근거에 있는 값만, 인용문은 원문에 실제로 있어야 한다."""

from __future__ import annotations

import re
from decimal import Decimal

from .amounts import MONEY_RE, NUMBER, money_values
from .catalog import squash

# 단위가 붙은 수치만 검사한다. '1) 2)'나 '3개' 같은 일반 숫자까지 막으면 정상 답도 떨어진다.
FIN_NUM_RE = re.compile(
    rf"(?<![\d.,])[+−-]?({NUMBER})\s*(%p|퍼센트\s*포인트|퍼센트|%)",
    re.IGNORECASE,
)
NEGATED_VALUE_RE = re.compile(
    r"\s*(?:(?:가|이|는|은)?\s*(?:아닙니다|아니(?:에요|고|라|며|다))|"
    r"라는\s*금리는\s*사실이\s*아닙니다)"
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


def asserted_unit_values(text: str, question: str = ""):
    """질문 수치를 바로 부정한 표현만 제외한다. 질문을 되풀이한 주장에는 면책이 없다."""
    ends = {
        m.start(): m.end()
        for pattern in (FIN_NUM_RE, MONEY_RE)
        for m in pattern.finditer(text)
    }
    questioned = {(kind, value) for _, kind, value in unit_values(question)}
    return [
        (pos, kind, value)
        for pos, kind, value in unit_values(text)
        if not (
            (kind, value) in questioned
            and NEGATED_VALUE_RE.match(text[ends[pos] :])
        )
    ]


def allowed_values(inp, query: str) -> dict[str, set[float]]:
    """답에 쓸 수 있는 수치를 단위별로 모은다. 기간(12개월)이 금리(12%)를 통과시키지 않도록 단위를 섞지 않는다.
    inp는 schemas.AnswerInput."""
    from .catalog import INTEREST_TAX

    pct = {round(r.value, 2) for r in inp.rows}
    won: set[float] = set()
    for c in inp.calculations:
        if c.inputs.get("product_type") != "loan":
            pct.add(round(INTEREST_TAX * 100, 2))  # 세후 계산에 쓴 이자소득세율
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
        if "|" in quote and not (
            c.get("section") == "금리표"
            and section_sources is not None
            and quote == section_sources.get(pid, {}).get("금리표")
        ):
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


# 우대조건 줄과 질문을 이어 줄 때 무시하는 일반어(모든 줄에 흔해 조건을 가르지 못한다).
CONDITION_STOP = {
    "우대",
    "우대금리",
    "우대율",
    "금리",
    "연",
    "조건",
    "경우",
    "해당",
    "상품",
    "적금",
    "예금",
    "이상",
    "각",
    "및",
    "또는",
    "개별",
    "전체",
    "합산",
    "최대",
    "최고",
    "기본",
    "기본금리",
    "최고금리",
    "얼마",
    "얼마야",
    "알려",
    "알려줘",
    "설명",
    "인가요",
    "몇",
    "폭",
    "만기",
    "보유",
    "가입",
    "충족",
    "아래",
}
CONDITION_SYNONYMS = {"월급": "급여", "소득이체": "급여"}


def _words(text: str) -> set[str]:
    """조건 대조용 단어(한글·영문 2자 이상, 일반어 제외)."""
    for word, canonical in CONDITION_SYNONYMS.items():
        text = text.replace(word, canonical)
    return {
        w
        for w in re.findall(r"[가-힣A-Za-z]{2,}", text)
        if w not in CONDITION_STOP
    }


def condition_score(text: str, line: str) -> int:
    """우대조건 한 줄의 단어가 text에 몇 개 나오는지. 한쪽이 다른 쪽에 포함되면 같은 단어로 본다
    ('모바일'과 '모바일뱅킹을', '급여'와 '급여이체랑')."""
    asked = _words(text)
    return sum(any(w in a or a in w for a in asked) for w in _words(line))


def condition_lines(documents) -> list[tuple[str, str, set[float]]]:
    """(product_id, 우대조건 줄, 그 줄의 금리 수치들). 수치가 있는 줄만."""
    return [
        (doc.product_id, line, vals)
        for doc in documents
        for desc in doc.descriptions
        if desc.section == "우대조건"
        for line in desc.text.splitlines()
        if (vals := values_of(line, "pct"))
    ]


def best_condition(text: str, lines) -> list[tuple[str, str, set[float]]]:
    """text와 단어가 가장 많이 겹치는 우대조건 줄들(겹침이 없으면 빈 목록)."""
    scored = [
        (condition_score(text, line), item)
        for item in lines
        for line in [item[1]]
    ]
    top = max((score for score, _ in scored), default=0)
    return [item for score, item in scored if top and score == top]


def condition_rate(query: str, documents) -> tuple[str, set[float]] | None:
    """개별 우대조건을 묻는 질문이면 (조건 이름, 우대율)을 돌려준다.
    질문 단어와 가장 많이 겹치는 우대조건 줄이 하나이고 그 줄의 수치가 하나일 때만 확정한다."""
    from .slots import EXCLUDED_ITEM_RE

    asked = re.sub(EXCLUDED_ITEM_RE, " ", query)
    if re.search(r"기본\s*금리|최고\s*금리|우대\s*(금리\s*)?폭", asked):
        return None
    if not re.search(r"우대|더\s*붙|얼마나\s*더|추가", asked):
        return None
    for doc in documents:
        for name in (doc.info.get("상품명", ""), doc.info.get("금융회사", "")):
            if name:
                asked = asked.replace(name, " ")
    best = best_condition(asked, condition_lines(documents))
    if len(best) != 1 or len(best[0][2]) != 1:
        return None
    line = re.sub(r"^\s*(?:[-*•·]+|\d+[.)]|[가-하][.)])?\s*", "", best[0][1])
    label = re.split(r"\s*[:：]", line)[0].strip() or line
    return f"'{label[:30]}' 조건 우대금리", set(best[0][2])


_TERM = r"\(?\s*\d[\d,]*(?:\.\d+)?\s*(?:%p|%|원|개월|년)?\s*\)?"
_OP = r"[×*÷/+−-]"
_EXPR = rf"{_TERM}(?:\s*{_OP}\s*{_TERM})*"
_LEFT_RE = re.compile(rf"({_EXPR})\s*$")
_RIGHT_RE = re.compile(rf"\s*({_EXPR})")
_VERBAL_RE = re.compile(
    r"더해|더하|더한|합하면|합산하면|합쳐|합친|차감하면|빼면|뺀"
)


def _evaluate(expr: str) -> Decimal | None:
    """숫자·사칙연산·괄호만 있는 식을 Decimal로 계산한다. 계산할 수 없으면 None."""
    tokens = re.findall(r"\d[\d,]*(?:\.\d+)?(?:%p|%)?|[×*÷/+−\-()]", expr)
    code = "".join(
        f"(Decimal('{t.rstrip('%p').replace(',', '')}')"
        + ("/100)" if t.endswith(("%", "%p")) else ")")
        if t[0].isdigit()
        else {"×": "*", "÷": "/", "−": "-"}.get(t, t)
        for t in tokens
    )
    try:
        value = eval(code, {"__builtins__": {}, "Decimal": Decimal})  # noqa: S307 - 숫자·연산자 토큰만 조립
    except (SyntaxError, ArithmeticError, TypeError):
        return None
    return value if isinstance(value, Decimal) else None


def _same(computed: Decimal, written: str, percent: bool, rough: bool) -> bool:
    """식의 값과 답에 적은 값이 같은지. 원 단위 절사·반올림과 적은 자릿수의 반올림은 허용한다.
    rough면 식에 반올림한 소수(예: 0.0020833)가 들어 있어 상대오차 0.05%까지 허용한다."""
    number = written.replace(",", "")
    places = len(number.split(".")[1]) if "." in number else 0
    value, step = Decimal(number), Decimal(1).scaleb(-places)
    if percent:
        value, step = value / 100, step / 100
    tolerance = max(
        step, abs(value) * Decimal("0.0005") if rough else Decimal(0)
    )
    return abs(computed - value) < tolerance


def check_equations(reply: str) -> tuple[list[str], set[tuple[str, float]]]:
    """답에 쓴 등식(A × B = C)을 직접 계산해 본다. (틀린 등식 목록, 맞은 등식의 단위 붙은 결과값).
    계산 과정 생략은 허용하지만, 쓴 계산이 틀리면 정답으로 인정하지 않는다."""
    wrong, derived = [], set()
    parts = re.split(r"[=＝]", reply)
    for left, right in zip(parts, parts[1:]):
        lhs, rhs = _LEFT_RE.search(left), _RIGHT_RE.match(right)
        if not (lhs and rhs and rhs[1].strip()):
            continue
        if re.search(r"[×*÷/+−\-()]$", left[: lhs.start()].rstrip()):
            continue  # '(연 이자율) ÷ 12'처럼 식의 앞부분이 숫자가 아니면 계산하지 않는다
        a, b = lhs[1].strip(), rhs[1].strip()
        if not re.search(_OP + r"\s*[\d(]", a + " " + b):
            continue
        if len({u for u in re.findall(r"개월|년", a + " " + b)}) > 1:
            continue  # '12.5개월 = 12.5/12년'처럼 단위 환산 식은 계산하지 않는다
        va, vb = _evaluate(a), _evaluate(b)
        if va is None or vb is None:
            continue
        rough = bool(re.search(r"\.\d{4,}", a + b))
        literal = re.fullmatch(rf"({NUMBER})\s*(%p|%|원|개월|년)?", b)
        if literal:
            same = _same(va, literal[1], "%" in (literal[2] or ""), rough)
        else:
            slack = abs(vb) * Decimal("0.0005") if rough else Decimal(0)
            same = abs(va - vb) <= max(Decimal("0.005"), slack)
        if not same:
            wrong.append(f"{a} = {b}")
        elif literal and literal[2] in ("%", "%p", "원"):
            kind = "won" if literal[2] == "원" else "pct"
            derived.add((kind, float(literal[1].replace(",", ""))))
    for sentence in re.split(r"(?<=[.!?。])\s+|\n", reply):
        if not _VERBAL_RE.search(sentence) or "=" in sentence:
            continue
        rates = [v for _, k, v in unit_values(sentence) if k == "pct"]
        if len(rates) >= 3 and not any(
            abs(op(rates[0], rates[1]) - rates[-1]) < 0.005
            for op in (lambda x, y: x + y, lambda x, y: x - y)
        ):
            wrong.append(sentence.strip()[:60])
    return wrong, derived


def check_answer(
    reply: str,
    allowed: dict[str, set[float]],
    citations: list[dict],
    sections: dict[str, str],
    required: list[tuple[float, ...]] = (),
    winner: tuple[list[list[str]], list[str]] | None = None,
    attribute: tuple[str, set[float]] | None = None,
    question: str = "",
) -> tuple[bool, str | None]:
    """확인된 핵심 오류만 반려한다. 표현·배치(표, 순서, 첫 수치)는 검사하지 않는다.
    allowed: allowed_values()의 단위별 허용 수치. sections: product_id -> 인용 가능한 원문.
    attribute: (질문이 묻는 값의 이름, 허용값) — 답 어딘가에 그 값이 있어야 한다.
    winner: (1위 상품별 이름 후보들, 전체 후보 이름) — 공동 1위는 모두 있어야 하고 '가장·1위' 주장은 1위 상품이어야 한다.
    반려 사유는 첫 실패에서 멈추지 않고 모두 모은다: 하나씩 고치다 재작성 한도에 걸리지 않게."""
    why: list[str] = []
    wrong, derived = check_equations(reply)
    bad = sorted(
        {
            f"{v:g}%" if k == "pct" else f"{v:,.0f}원"
            for _, k, v in asserted_unit_values(reply, question)
            if v not in allowed[k] and (k, v) not in derived
        }
    )
    if bad:
        why.append(
            f"근거에 없는 수치 {bad}. 조회·계산 결과와 문서에 있는 숫자만 그대로 쓰세요(반올림·환산 금지)"
        )
    if wrong:
        why.append(
            f"계산·합산 설명이 틀렸습니다: {wrong}. 틀린 식은 빼거나 계산 결과만 쓰세요"
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
        stated = {
            v for _, k, v in asserted_unit_values(reply, question) if k == "pct"
        }
        if not stated & vals:
            unit = "%p" if "우대" in name and "포함" not in name else "%"
            why.append(
                f"질문한 {name}({', '.join(f'{v:g}{unit}' for v in sorted(vals))})이 답에 없습니다"
            )
    if winner:
        why += _winner_errors(reply, *winner)
    got = {
        v
        for _, kind, v in asserted_unit_values(reply, question)
        if kind == "won"
    }
    missing = [
        g for g in required if not any(round(float(v), 2) in got for v in g)
    ]  # 세전·세후 중 하나면 충분
    if missing:
        why.append(
            f"계산 결과 {[f'{int(g[0]):,}원' for g in missing]}(또는 세후 금액)이 답에 없음"
        )
    if not why:
        return True, None
    return False, why[0] if len(why) == 1 else "\n".join(
        f"{i}. {w}" for i, w in enumerate(why, 1)
    )


SUPERLATIVE_RE = r"가장|제일|1위|최고로|최저로"


def _winner_errors(
    reply: str, tops: list[list[str]], candidates: list[str]
) -> list[str]:
    """공동 1위 누락과 '가장·1위'를 1위가 아닌 상품에 붙인 주장만 오류로 본다(서술 순서는 보지 않는다)."""
    text = squash(reply)
    errors = []
    absent = [
        names[0] for names in tops if not any(squash(n) in text for n in names)
    ]
    if absent:
        errors.append(
            f"조회 1위 {', '.join(absent)}이(가) 답에 없습니다. 공동 1위는 모두 쓰세요"
        )
    top_names = {squash(n) for names in tops for n in names}
    for sentence in re.split(r"(?<=[.!?。])\s+|\n", reply):
        if re.search(SUPERLATIVE_RE, sentence):
            picked = first_mention(sentence, candidates)
            if picked and picked not in top_names:
                errors.append(
                    f"'{sentence.strip()[:40]}'에서 1위가 아닌 상품을 1위로 설명했습니다"
                )
    return errors
