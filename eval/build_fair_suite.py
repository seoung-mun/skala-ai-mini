"""새 비교셋: raw JSON 직접 조회와 독립 Decimal 수식으로 정답을 만든다."""

import json
from decimal import ROUND_FLOOR, ROUND_HALF_UP
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval/fair_suite_20261021.jsonl"


def raw(kind):
    p = ROOT / f"data/raw/{kind}ProductsSearch_bank.json"
    d = json.loads(p.read_text())
    return p.name, d.get("result", d)


def product(kind, name):
    file, data = raw(kind)
    bases = [b for b in data["baseList"] if b["fin_prdt_nm"] == name]
    assert len(bases) == 1, (kind, name)
    b = bases[0]
    opts = [
        o
        for o in data["optionList"]
        if all(o[k] == b[k] for k in ("dcls_month", "fin_co_no", "fin_prdt_cd"))
    ]
    return (
        b,
        opts,
        {
            "file": file,
            "key": {
                k: b[k] for k in ("dcls_month", "fin_co_no", "fin_prdt_cd")
            },
        },
    )


def rate(kind, name, term, field):
    b, opts, src = product(kind, name)
    opts = [
        o
        for o in opts
        if int(o["save_trm"]) == term and o["intr_rate_type_nm"] == "단리"
    ]
    values = {o[field] for o in opts if o[field] is not None}
    assert len(values) == 1, (name, term, values)
    return (
        float(values.pop()),
        {
            **src,
            "term": term,
            "interest": "단리",
            "field": field,
            "raw_options": opts,
        },
        b,
    )


def floor(x):
    return int(x.to_integral_value(rounding=ROUND_FLOOR))


def make_suite():
    items = []

    def add(category, query, rubric, source, history=None):
        items.append(
            {
                "id": f"Q{len(items) + 1:02}",
                "category": category,
                "query": query,
                "history": history or [],
                "rubric": rubric,
                "source": source,
            }
        )

    chosen = [
        ("deposit", "WON플러스예금", 24, "intr_rate"),
        ("deposit", "iM스마트예금", 24, "intr_rate2"),
        ("deposit", "LIVE정기예금", 12, "intr_rate"),
        ("deposit", "더(The) 특판 정기예금", 24, "intr_rate2"),
        ("deposit", "스마트모아Dream정기예금", 24, "intr_rate"),
        ("deposit", "굿스타트예금", 12, "intr_rate2"),
        ("saving", "WON적금", 12, "intr_rate"),
        ("saving", "iM함께적금", 12, "intr_rate2"),
        ("saving", "펫 적금", 12, "intr_rate"),
        ("saving", "저탄소 실천 적금", 24, "intr_rate2"),
        ("saving", "부산이라 좋다\nBig적금", 12, "intr_rate"),
        ("saving", "Only One 주거래 우대적금", 12, "intr_rate2"),
    ]
    for kind, name, term, field in chosen:
        value, src, b = rate(kind, name, term, field)
        label = "기본금리" if field == "intr_rate" else "우대 포함 최고금리"
        title = " ".join(name.split())
        add(
            "rate_fact",
            f"{b['kor_co_nm']} {title} {term}개월 단리의 {label}는 몇 %인가요? 해당 금리만 알려주세요.",
            {"type": "number", "value": value, "unit": "%", "subject": title},
            src,
        )

    for kind, term, field in [
        ("deposit", 6, "intr_rate"),
        ("deposit", 24, "intr_rate2"),
        ("saving", 6, "intr_rate"),
        ("saving", 24, "intr_rate2"),
        ("deposit", 36, "intr_rate"),
        ("saving", 36, "intr_rate"),
    ]:
        file, data = raw(kind)
        candidates = []
        for b in data["baseList"]:
            opts = [
                o
                for o in data["optionList"]
                if o["fin_co_no"] == b["fin_co_no"]
                and o["fin_prdt_cd"] == b["fin_prdt_cd"]
                and int(o["save_trm"]) == term
                and o[field] is not None
                and o["intr_rate_type_nm"] == "단리"
            ]
            if opts:
                candidates.append(
                    {
                        "bank": b["kor_co_nm"],
                        "name": " ".join(b["fin_prdt_nm"].split()),
                        "value": max(o[field] for o in opts),
                    }
                )
        best = max(c["value"] for c in candidates)
        winners = [c for c in candidates if c["value"] == best]
        label = "기본금리" if field == "intr_rate" else "우대 포함 최고금리"
        add(
            "ranking",
            f"저축은행은 제외하고 은행 {'정기예금' if kind == 'deposit' else '적금'} 중 {term}개월 단리 {label}가 가장 높은 상품과 금리를 알려주세요. 공동 1위는 모두 알려주세요.",
            {
                "type": "ranking",
                "value": best,
                "unit": "%",
                "winners": winners,
                "candidates": candidates,
            },
            {
                "file": file,
                "field": field,
                "term": term,
                "interest": "단리",
                "group": "bank",
            },
        )

    for kind, amount, term, rate_pct, after in [
        ("deposit", 17000000, 18, "3.2", False),
        ("deposit", 23000000, 24, "2.8", True),
        ("saving", 370000, 18, "3.2", False),
        ("saving", 650000, 24, "2.8", True),
    ]:
        interest = floor(
            D(amount)
            * D(rate_pct)
            * D(term)
            / 1200
            * (D(term + 1) / 2 if kind == "saving" else 1)
        )
        result = floor(D(interest) * D("0.846")) if after else interest
        tax = "세후(세율 15.4%)" if after else "세전"
        q = f"{'매달' if kind == 'saving' else '원금'} {amount:,}원을 {'적금에 납입' if kind == 'saving' else '예금에 예치'}하고 연 {rate_pct}%, {term}개월 단리라면 {tax} 이자는 몇 원인가요? 원 미만은 버려주세요."
        add(
            "calculation",
            q,
            {"type": "number", "value": result, "unit": "원"},
            {
                "formula": "P*r*T/1200 * ((T+1)/2 for saving); floor gross, then floor(gross*0.846)",
                "inputs": [kind, amount, term, rate_pct, after],
            },
        )
    for repayment in ["equal", "bullet"]:
        amount, months, rate_pct = 73000000, 180, D("4.2")
        r = rate_pct / 1200
        result = int(
            (
                D(amount) * r / (1 - (1 + r) ** (-months))
                if repayment == "equal"
                else D(amount) * r
            ).to_integral_value(rounding=ROUND_HALF_UP)
        )
        label = (
            "원리금균등상환 월 상환액"
            if repayment == "equal"
            else "만기일시상환 월 이자"
        )
        add(
            "calculation",
            f"대출 원금 7,300만원, 연 4.2%, 180개월일 때 {label}은 몇 원인가요? 원 단위 반올림으로 알려주세요.",
            {"type": "number", "value": result, "unit": "원"},
            {
                "formula": "round_half_up(P*r/(1-(1+r)^-T)) or round_half_up(P*r)",
                "inputs": [amount, months, str(rate_pct), repayment],
            },
        )
    for kind in ["deposit", "saving"]:
        target, months, rp = 12000000, 18, D("3.2")

        def maturity(amount):
            gross = floor(
                D(amount)
                * rp
                * months
                / 1200
                * (D(months + 1) / 2 if kind == "saving" else 1)
            )
            return amount * (months if kind == "saving" else 1) + floor(
                D(gross) * D("0.846")
            )

        lo, hi = 0, target
        while lo < hi:
            mid = (lo + hi) // 2
            if maturity(mid) >= target:
                hi = mid
            else:
                lo = mid + 1
        assert maturity(lo) >= target and maturity(lo - 1) < target
        label = "매월 적금 납입액" if kind == "saving" else "예금 원금"
        add(
            "inverse",
            f"연 3.2% 단리, 18개월, 이자 세율 15.4%로 세후 만기 수령액 1,200만원 이상을 만들려면 최소 {label}은 몇 원인가요? 이자와 세후 이자는 각각 원 미만 버림으로 계산해주세요.",
            {"type": "number", "value": lo, "unit": "원"},
            {
                "formula": "independent Decimal maturity + integer lower_bound",
                "inputs": [kind, target, months, str(rp)],
                "boundary": [maturity(lo - 1), maturity(lo)],
            },
        )

    for name, phrase, value, terms in [
        ("WON적금", "타행계좌 오픈뱅킹 등록", 0.1, ["오픈뱅킹", "타행"]),
        ("iM함께적금", "인터넷 또는 모바일 가입", 0.05, ["인터넷|모바일"]),
        ("우리SUPER주거래적금", "공과금 자동이체", 0.3, ["공과금"]),
    ]:
        b, _, src = product("saving", name)
        add(
            "condition",
            f"{b['kor_co_nm']} {name}의 {phrase} 조건에 해당하는 개별 우대금리는 몇 %p인가요? 기본금리나 전체 우대 폭은 빼고 해당 조건만 설명해주세요.",
            {"type": "number", "value": value, "unit": "%p", "terms": terms},
            {**src, "field": "spcl_cnd", "text": b["spcl_cnd"]},
        )
    b, _, src = product("saving", "우리SUPER주거래적금")
    add(
        "condition",
        "우리SUPER주거래적금 우대조건 알려줘",
        {
            "type": "conditions",
            "terms": [
                "급여|연금",
                "공과금",
                "카드",
                "마케팅",
                "1/2|절반|반 이상",
            ],
            "rates": [0.7, 0.3, 0.1],
        },
        {**src, "field": "spcl_cnd", "text": b["spcl_cnd"]},
    )

    for name, term, newterm, field in [
        ("WON플러스예금", 12, 24, "intr_rate"),
        ("iM스마트예금", 12, 24, "intr_rate2"),
        ("더(The) 특판 정기예금", 12, 24, "intr_rate2"),
        ("스마트모아Dream정기예금", 12, 24, "intr_rate"),
        ("저탄소 실천 적금", 12, 24, "intr_rate2"),
        ("우리SUPER주거래적금", 12, 24, "intr_rate"),
    ]:
        kind = "saving" if "적금" in name else "deposit"
        before, _, b = rate(kind, name, term, field)
        value, src, _ = rate(kind, name, newterm, field)
        label = "기본금리" if field == "intr_rate" else "우대 포함 최고금리"
        history = [
            {
                "role": "user",
                "content": f"{b['kor_co_nm']} {name} {term}개월 단리 {label} 알려주세요.",
            },
            {
                "role": "assistant",
                "content": f"{b['kor_co_nm']} {name} {term}개월 단리 {label}는 {before}%입니다.",
            },
        ]
        add(
            "follow_up",
            f"그 상품 {newterm}개월은요? 같은 금리 항목만 알려주세요.",
            {"type": "number", "value": value, "unit": "%", "subject": name},
            src,
            history,
        )
    add(
        "boundary",
        "신용점수는 아직 모르겠어요. 은행 신용대출 중 제게 적용될 금리가 가장 낮은 상품과 그 금리를 확정해줘.",
        {"type": "clarify", "terms": ["신용|점수"], "no_rate": True},
        {
            "reason": "score and personal eligibility unspecified; cannot determine personal rate"
        },
    )
    add(
        "boundary",
        "내년 이맘때 우리은행 예금 금리가 정확히 몇 %인지 확정해서 알려줘.",
        {
            "type": "clarify",
            "terms": ["예측|확정|미래|알 수|확인"],
            "no_rate": True,
        },
        {"reason": "future rate unavailable from frozen disclosure"},
    )
    add(
        "boundary",
        "은행 정기예금의 777개월 공시 기본금리를 조회해줘. 다른 기간으로 바꾸지 말아줘.",
        {"type": "clarify", "terms": ["없|불가|확인|찾|공시"], "no_rate": True},
        {"reason": "no raw deposit option has save_trm=777"},
    )
    add(
        "boundary",
        "지금 비트코인을 사야 할까? 이번 주 수익률을 확정해줘.",
        {
            "type": "clarify",
            "terms": ["범위|제공|확정|예측|어렵|금융상품|공시"],
            "no_rate": True,
        },
        {"reason": "out of service disclosure scope"},
    )
    assert len(items) == 40
    return items


if __name__ == "__main__":
    if OUT.exists():
        raise SystemExit("기존 새 평가셋을 덮어쓰지 않습니다")
    items = make_suite()
    OUT.write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items)
    )
    print(f"Created {len(items)} items: {OUT}")
