"""규칙 기반 슬롯 추출기. 런타임은 쓰지 않는다: 가짜 라우터(테스트)와 과거 공정 평가 재현용이다."""

from __future__ import annotations

import re

from finagent.catalog import (
    BANK_ALIASES,
    CREDIT_TYPE,
    Catalog,
    squash,
)
from finagent.slots import EXCLUDED_ITEM_RE, Slots, months, won_amounts

KIND_RE = [
    ("credit", r"신용대출|마이너스|신용점수|카드론"),
    ("rent", r"전세"),
    ("mortgage", r"주담대|주택담보|담보대출|아파트\s*담보|집\s*담보"),
    ("saving", r"적금"),
    ("deposit", r"예금"),
]
OUT_OF_SCOPE_RE = (
    r"주식|주가|코인|비트코인|이더리움|보험|날씨|환율|펀드|로또|부동산\s*시세"
)
FINANCE_RE = r"금리|이자|우대|대출|예금|적금|은행|뱅크|가입|상품|수수료|한도|상환|신용|저축|빌리|빌려|갚"
COMPARE_RE = r"제일|가장|비교|중에|중\s|순서|순위|추천|어디|어느|뭐가\s*나아|더\s*높|더\s*낮|\d+\s*개\s*(보여|알려)"
# 최대 우대 폭(%p)을 묻는 질문 — 최고금리(%)와 구분. "자동이체하면 몇 %p?"처럼 조건 하나를 묻는 질문은 제외
SPREAD_RE = r"우대\s*(금리\s*)?폭|(최고|최대).*(%\s*p|퍼센트\s*포인트)"
# "저축은행은 제외하고", "은행 말고" 같은 금융권 제외 표현
EXCLUDE_RE = r"\s*(?:은|는|을|를|이|가|도)?\s*(?:제외|빼고|말고|아닌)"
# 미래 시점 금리를 확정·예측해 달라는 질문. 공시는 현재 값만 있어 답할 수 없다.
FUTURE_RE = (
    r"(?:내년|다음\s*(?:달|해|분기)|\d+\s*(?:개월|년)\s*(?:뒤|후)|앞으로|미래|이맘때)"
    r".*(?:금리|이율).*(?:확정|정확히|예측|오를|오르|내릴|떨어|될지|바뀔)"
)
GOAL_RE = r"모으려면|모으고\s*싶|모을\s*수|만들려면|목표|얼마까지\s*빌|최대\s*얼마.*빌|갚을\s*수\s*있"  # 역산 계산
CALC_RE = r"이자\s*(얼마|는)|얼마나\s*(돼|받|붙)|상환액|한\s*달에\s*얼마|월\s*얼마|갚으면|빌리면|빌려서|넣으면|세전이자|만기에\s*얼마"


def _group(q: str) -> str | None:
    """금융권(bank·savingsbank)을 고른다. 제외 표현이 붙은 금융권은 고르지 않는다."""
    savings = re.search(r"저축은행", q)
    savings_out = bool(re.search("저축은행" + EXCLUDE_RE, q))
    bank_out = bool(re.search(r"(?<!저축)은행" + EXCLUDE_RE, q))
    if savings and not savings_out:
        return "savingsbank"
    if savings_out and bank_out:
        raise ValueError(
            "은행과 저축은행을 모두 제외하면 조회할 금융권이 없습니다"
        )
    if savings_out or re.search(r"(^|\s)은행", q):
        return None if bank_out else "bank"
    return None


def extract(q: str, catalog: Catalog) -> Slots:
    """질문에서 Slots를 규칙으로 뽑는다(LLM 없음)."""
    kinds = [k for k, pat in KIND_RE if re.search(pat, q)]
    prods = catalog.find_products(q)
    for p in prods:
        if p.kind not in kinds:
            kinds.append(p.kind)
    amounts = won_amounts(q)
    score = next(
        (
            int(s)
            for s in re.findall(r"(\d{3,4})\s*점", q)
            if 0 <= int(s) <= 1000
        ),
        None,
    )
    rate = re.search(r"(\d+(?:\.\d+)?)\s*%(?!\s*[pP])", q)
    group = _group(q)
    metric = None
    asked = re.sub(EXCLUDED_ITEM_RE, " ", q)
    if any(k in ("saving", "deposit") for k in kinds):
        metric = (
            "spread"
            if re.search(SPREAD_RE, asked)
            else (
                "rate"
                if "기본" in asked
                else (
                    "rate_max"
                    if re.search(r"최고|우대(?!\s*조건)", asked)
                    else None
                )
            )
        )
    elif re.search(r"최고\s*금리|최대\s*금리", asked):
        metric = "max"
    elif re.search(r"최저\s*금리", asked):
        metric = "min"
    f = {}
    for word, key, val in (
        ("아파트외", "mrtg_type_nm", "아파트외"),
        ("분할", "rpay_type_nm", "분할상환방식"),
        ("만기일시", "rpay_type_nm", "만기일시상환방식"),
        ("고정", "lend_rate_type_nm", "고정금리"),
        ("변동", "lend_rate_type_nm", "변동금리"),
        ("정액", "rsrv_type_nm", "정액적립식"),
        ("자유적립", "rsrv_type_nm", "자유적립식"),
    ):
        if word in q:
            f.setdefault(key, val)
    if "아파트" in q and "mrtg_type_nm" not in f:
        f["mrtg_type_nm"] = "아파트"
    if "credit" in kinds:
        f["crdt_prdt_type"] = next(
            (v for w, v in CREDIT_TYPE.items() if w in q and w != "일반"), "1"
        )
        prods = [
            p
            for p in prods
            if p.kind != "credit"
            or p.base["crdt_prdt_type"] == f["crdt_prdt_type"]
        ]
    term = months(q)
    # 'SC제일은행'의 '제일'처럼 이름 속 단어가 비교어로 잡히지 않게 이름을 지우고 신호를 찾는다.
    cue = q
    for name in (
        [p.name for p in prods] + catalog.find_banks(q) + list(BANK_ALIASES)
    ):
        cue = cue.replace(name, " ")
    distinct = {squash(p.name) for p in prods}
    return Slots(
        question=q,
        kinds=kinds,
        group=group,
        amount_won=amounts[0] if amounts else None,
        monthly=bool(re.search(r"(월|매달|매월)\s*\d", q)),
        term_months=term,
        credit_score=score,
        rate_pct=float(rate.group(1)) if rate else None,
        banks=catalog.find_banks(q),
        product_ids=[p.id for p in prods],
        metric=metric,
        filters=f,
        compare=bool(re.search(COMPARE_RE, cue)) or len(distinct) >= 2,
        calc=bool(re.search(CALC_RE, cue)) and bool(amounts),
        goal=bool(re.search(GOAL_RE, cue)) and bool(amounts),
    )
