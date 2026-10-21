"""금융 수치를 상품·옵션·속성·계산 전제에 연결하는 출처 검사."""

import re

from .amounts import MONEY_RE
from .catalog import CATEGORY_RE, INTEREST_TAX, squash
from .validate import (
    FIN_NUM_RE,
    NEGATED_VALUE_RE,
    asserted_unit_values,
    best_condition,
    condition_lines,
    condition_rate,
    unit_values,
    values_of,
)

_RATE_LABELS = {
    "기본금리": "base",
    "기본": "base",
    "최고금리": "max",
    "최고": "max",
    "우대포함": "max",
    "최저금리": "min",
    "최저": "min",
    "우대폭": "spread",
    "우대금리": "spread",
    "우대율": "spread",
    "대출금리": "loan",
    "기준금리": "benchmark",
    "가산금리": "extra",
    "가감조정금리": "adjustment",
}
_RATE_LABEL_RE = re.compile("|".join(_RATE_LABELS))
_OPTION_RE = re.compile(
    r"\d+개월|단리|복리|정액적립식|자유적립식|"
    r"원리금분할상환|원금분할상환|만기일시상환|고정금리|변동금리"
)


def _rate_metric(label: str) -> str | None:
    match = _RATE_LABEL_RE.search(squash(label))
    return _RATE_LABELS[match[0]] if match else None


def _show(value: float, unit: str) -> str:
    """오류 문구용 수치 표기(금액은 쉼표, 금리는 그대로)."""
    return f"{value:,.0f}원" if unit == "원" else f"{value:g}{unit}"


def check_provenance(inp, reply: str, notes: list | None = None) -> list[str]:
    """금융 수치의 대상(상품)·조건(기간·방식·우대조건)·항목(기본/최고·세전/세후)·단위를 확인한다.
    돌려주는 것은 '확인된 오류'(근거가 다른 값을 말함)뿐이다. 검사기가 수치를 근거에 연결하지
    못한 경우는 오류가 아니라 notes에 남긴다(정상 답을 잘못 반려하지 않기 위해)."""
    from .slots import months

    text = re.sub(
        r"[^\S\n]+", "", reply.replace("**", "").replace("`", "")
    ).lower()
    docs = {d.product_id: d for d in inp.documents}
    rates = [*inp.rows, *(r for d in inp.documents for r in d.rates)]
    names: dict[str, set[str]] = {}
    for pid in {r.product_id for r in rates} | set(docs):
        row = next((r for r in rates if r.product_id == pid), None)
        doc = docs.get(pid)
        bank = row.bank if row else doc.info.get("금융회사", "")
        product = row.product if row else doc.info.get("상품명", doc.title)
        bank_alias = bank.replace("주식회사", "").strip()
        for name in (
            product if not re.fullmatch(CATEGORY_RE, squash(product)) else "",
            f"{bank} {product}",
            f"{bank_alias} {product}",
            bank,
            bank_alias,
        ):
            if name:
                names.setdefault(squash(name), set()).add(pid)
    pattern = "|".join(
        re.escape(n) for n in sorted(names, key=len, reverse=True)
    )
    mentions = list(re.finditer(pattern, text)) if pattern else []
    units = {
        m.start(): (
            m.end(),
            "%p" if "p" in m[2].lower() or "포인트" in m[2] else "%",
        )
        for m in FIN_NUM_RE.finditer(text)
    }
    units.update({m.start(): (m.end(), "원") for m in MONEY_RE.finditer(text)})
    errors = []
    previous_end = 0
    asserted_positions = {
        pos for pos, _, _ in asserted_unit_values(text, squash(inp.question))
    }
    for pos, kind, value in unit_values(text):
        end, unit = units[pos]
        paragraph_start = (
            text.rfind("\n\n", 0, pos) + 2 if "\n\n" in text[:pos] else 0
        )
        preceding = [
            m
            for m in mentions
            if paragraph_start <= m.start() and m.end() <= pos
        ]
        mention = preceding[-1] if preceding else None
        pids = (
            names[mention[0]]
            if mention
            else set(docs) | {r.product_id for r in inp.rows}
        )
        joint_mentions = [
            m
            for m in preceding
            if m.start() >= max(previous_end, text.rfind("\n", 0, pos) + 1)
        ]
        if len(joint_mentions) > 1:
            pids = set.union(*(names[m[0]] for m in joint_mentions))
        start = mention.end() if mention else 0
        block = text[start:pos]
        clause = text[
            max(
                paragraph_start, previous_end, text.rfind("\n", 0, pos) + 1
            ) : pos
        ]
        previous_end = end
        labels = list(_RATE_LABEL_RE.finditer(clause))
        metric = _RATE_LABELS[labels[-1][0]] if labels else None
        if (
            unit == "%p"
            and "우대" in clause
            and all(
                m[0] == "최고" or _RATE_LABELS[m[0]] == "spread" for m in labels
            )
        ):
            metric = "spread"
        terms = re.findall(r"\d+개월", block)
        option_words = set(_OPTION_RE.findall(block))
        option_words -= set(terms)
        if terms:
            option_words.add(terms[-1])

        def from_document(pid):
            doc = docs.get(pid)
            if not doc:
                return False
            for source in [
                *(d.text for d in doc.descriptions),
                *(str(v) for v in doc.info.values()),
            ]:
                for line in source.splitlines():
                    quoted = squash(line)
                    if not quoted:
                        continue
                    if metric is not None and _rate_metric(quoted) != metric:
                        continue
                    for occurrence in re.finditer(re.escape(quoted), text):
                        if occurrence.start() <= pos < occurrence.end():
                            return True
            if not (
                kind == "pct" and unit == "%p" and metric in (None, "spread")
            ):
                return False
            target = condition_rate(clause, [doc]) or condition_rate(
                inp.question, [doc]
            )
            if target and value in target[1]:
                return True
            # 의역한 우대조건: 설명과 가장 많이 겹치는 우대조건 줄의 우대율이면 인정한다.
            lines = condition_lines([doc])
            best = best_condition(clause, lines) or lines
            return any(value in vals for _, _, vals in best)

        def from_rate(pid):
            selected = [r for r in inp.rows if r.product_id == pid]
            candidates = [r for r in rates if r.product_id == pid]
            if metric is None and selected:
                candidates = selected
            if not terms and selected:
                candidates = [
                    r
                    for r in candidates
                    if r.option in {s.option for s in selected}
                ]
            return any(
                round(r.value, 2) == value
                and r.unit == unit
                and (metric is None or _rate_metric(r.metric) == metric)
                and all(word in squash(r.option) for word in option_words)
                and (
                    terms
                    or not re.search(r"\d+개월", squash(r.option))
                    or (term := months(inp.question)) is None
                    or f"{term}개월" in squash(r.option)
                )
                for r in candidates
            )

        def from_calculation(calc):
            line_start = text.rfind("\n", 0, pos) + 1
            # 완결된 금리 목록 다음 계산 문장은 별도 문맥이고, 상품명만 쓴 제목은 이어진다.
            calculation_pids = (
                pids
                if mention and not unit_values(text[mention.end() : line_start])
                else set()
            )
            if (
                calc.rate_from
                and calculation_pids
                and not calculation_pids.issubset(
                    set().union(
                        *(
                            names[m[0]]
                            for m in re.finditer(
                                pattern, squash(calc.rate_from)
                            )
                        )
                    )
                )
            ):
                return False
            inputs = calc.inputs
            if terms and terms[-1] != f"{inputs.get('term_months')}개월":
                return False
            interest = (
                "복리" if inputs.get("interest_type") == "compound" else "단리"
            )
            if option_words & {"단리", "복리"} - {interest}:
                return False
            goal = inputs.get("mode") == "goal"
            if kind == "pct" and value == round(INTEREST_TAX * 100, 2):
                return inputs.get("product_type") != "loan" and bool(
                    re.search(
                        r"세율|세금|이자소득세|과세|원천징수|세후",
                        clause + text[end : end + 20],
                    )
                )
            if kind == "pct":
                return (
                    unit == "%"
                    and metric is None
                    and value == inputs.get("rate_pct")
                    and (
                        calc.rate_from
                        or not calculation_pids
                        or re.search(
                            r"가정|적용|계산|전제",
                            clause + re.split(r"[.!?\n]", text[end:])[0],
                        )
                    )
                )
            if re.search(
                r"만기수령액|만기금액|최종잔액|총수령액|원리금(?!균등|분할)",
                clause,
            ):
                # 역산(goal)에서는 만기 수령액이 사용자가 정한 목표 금액이다
                return goal and value == inputs.get("amount_won")
            labels = list(
                re.finditer(
                    r"세전|세후|원금|월납입|매월|목표|이자|상환액", clause
                )
            )
            label = labels[-1][0] if labels else None
            tax = list(re.finditer(r"세전|세후", clause))
            if tax:
                label = tax[-1][0]
            if label == "세후":
                return value == calc.after_tax
            if label == "세전":
                return (
                    value == calc.result
                    and inputs.get("mode") != "goal"
                    and inputs.get("product_type") != "loan"
                )
            if goal and label in ("원금", "월납입", "매월"):
                return (
                    value == calc.result
                )  # 역산 결과가 필요 원금·월 납입액이다
            if label in ("원금", "월납입", "매월", "목표"):
                return value == inputs.get("amount_won")
            return (
                value in (calc.result, calc.after_tax, inputs.get("amount_won"))
                if label is None
                else value in (calc.result, calc.after_tax)
            )

        supported = (
            pids
            and all(
                from_document(pid) or (kind == "pct" and from_rate(pid))
                for pid in pids
            )
        ) or any(from_calculation(c) for c in inp.calculations)
        if NEGATED_VALUE_RE.match(text[end:]):
            if (
                pos not in asserted_positions
                and not supported
                and any(r.product_id in pids for r in rates)
            ):
                continue
            errors.append(f"부정 표현의 근거 불일치: {_show(value, unit)}")
            continue
        if supported:
            continue
        error = _confirmed_error(
            inp,
            kind,
            value,
            unit,
            clause,
            pids,
            mention,
            metric,
            option_words,
            rates,
            docs,
        )
        if error:
            errors.append(error)
        elif notes is not None:
            notes.append(f"근거와 연결하지 못한 수치: {_show(value, unit)}")
    return list(dict.fromkeys(errors))


def _confirmed_error(
    inp,
    kind,
    value,
    unit,
    clause,
    pids,
    mention,
    metric,
    option_words,
    rates,
    docs,
) -> str | None:
    """연결에 실패한 수치가 '근거가 다른 값을 말하는' 확인된 오류인지 판정한다. 아니면 None(해석 못 함)."""
    show = _show(value, unit)
    roles = r"세전|세후|원금|월납입|매월|이자|상환액|수령액|원리금|총액"
    if kind == "won":
        for c in inp.calculations:
            known = {c.result, c.after_tax, c.inputs.get("amount_won")} - {None}
            if value in known and re.search(roles, clause):
                return f"계산 항목 불일치: {show}. 세전·세후·원금·월 납입액 등 계산 결과의 항목을 바꾸지 마세요."
        sources = [
            line
            for pid in pids
            if pid in docs
            for text in [
                *(d.text for d in docs[pid].descriptions),
                *(str(v) for v in docs[pid].info.values()),
            ]
            for line in text.splitlines()
            if value in values_of(line, "won")
        ]
        claimed = set(re.findall(roles, clause))
        if (
            mention
            and sources
            and claimed
            and not any(
                word in squash(line) for word in claimed for line in sources
            )
        ):
            return (
                f"항목 불일치: {show}. 공시 원문에서 이 금액은 다른 항목입니다."
            )
        return None

    def rate_values(pid):
        return {
            round(r.value, 2)
            for r in rates
            if r.product_id == pid and r.unit == unit
        }

    def doc_values(pid):
        doc = docs.get(pid)
        if not doc:
            return set()
        texts = [d.text for d in doc.descriptions] + [
            str(v) for v in doc.info.values()
        ]
        return (
            set().union(*(values_of(t, "pct") for t in texts))
            if texts
            else set()
        )

    if not mention:
        return None
    every = {r.product_id for r in rates} | set(docs)
    if not any(value in rate_values(pid) | doc_values(pid) for pid in every):
        return f"근거 없는 상품 수치: {show}. 이 상품의 공시·조회 결과에 없는 값입니다."
    if unit == "%p" and metric not in (None, "spread"):
        return f"항목 불일치: {show}. 우대율(%p)을 기본·최고금리로 설명하지 마세요."
    other_unit = "%" if unit == "%p" else "%p"
    lines = [x for x in condition_lines(inp.documents) if x[0] in pids]
    if any(
        round(r.value, 2) == value and r.unit == other_unit
        for r in rates
        if r.product_id in pids
    ) and not any(value in vals for _, _, vals in lines):
        return f"단위 불일치: {show}. 금리는 %, 우대 폭·우대율은 %p로 쓰세요."
    if unit == "%p" and any(value in doc_values(pid) for pid in pids):
        best = best_condition(clause, lines)
        if best and all(value not in vals for _, _, vals in best):
            return f"우대조건과 우대율 불일치: {show}. 설명한 조건에 해당하는 공시 우대율을 쓰세요."
        return None
    own = any(value in rate_values(pid) | doc_values(pid) for pid in pids)
    if own and (metric is not None or option_words):
        return f"항목·조건 불일치: {show}. 이 상품에서 이 값은 다른 금리 종류·기간·방식의 값입니다."
    if not own and any(
        value in rate_values(pid) | doc_values(pid) for pid in every - pids
    ):
        return f"대상 불일치: {show}. 이 값은 다른 상품의 공시값입니다."
    return None
