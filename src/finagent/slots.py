"""질문에서 슬롯과 라우팅 신호를 규칙으로 뽑는다. LLM 없이 재현 가능해야 라우팅 정확도를 측정할 수 있다."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .amounts import money_values
from .catalog import BANK_ALIASES, CREDIT_TYPE, Catalog, squash

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
GOAL_RE = r"모으려면|모으고\s*싶|모을\s*수|만들려면|목표|얼마까지\s*빌|최대\s*얼마.*빌|갚을\s*수\s*있"  # 역산 계산
CALC_RE = r"이자\s*(얼마|는)|얼마나\s*(돼|받|붙)|상환액|한\s*달에\s*얼마|월\s*얼마|갚으면|빌리면|빌려서|넣으면|세전이자|만기에\s*얼마"


def asks_recall(q: str) -> bool:
    """자신의 이전 질문 원문을 묻는 명확한 회상 요청만 잡는다."""
    return bool(
        re.search(r"방금|직전|이전|아까|앞서|마지막|내가|제가|내 |제 ", q)
        and re.search(
            r"(?:뭐|무엇|어떤|무슨).*(?:질문했|물었|물어봤|요청했)|"
            r"(?:질문|요청)(?:\s*(?:내용|원문|을|은|이))?\s*"
            r"(?:뭐(?:야|였|지)|무엇(?:이야|이었|이지)|(?:어떤|무슨).*였|다시|알려|기억|말해|반복)",
            q,
        )
    )


def won_amounts(text: str) -> list[int]:
    """복합 금액·축약 금액을 원 단위로 읽으며 기간·금리는 포함하지 않는다."""
    values = [v for _, v in money_values(text)]
    if any(v != v.to_integral_value() for v in values):
        raise ValueError("원 미만의 금액은 지원하지 않습니다")
    return [int(v) for v in values]


def months(text: str) -> int | None:
    """공시 날짜를 제외한 첫 가입 기간을 개월로 바꾼다."""
    text = re.sub(
        r"(?<!\d)(?:19|20|21)\d{2}\s*년(?:\s*\d{1,2}\s*월)?(?:\s*\d{1,2}\s*일)?",
        "",
        text,
    )
    match = re.search(
        r"(?<![\d.])(?:(\d+)\s*년(?:\s*(?:(\d+)\s*개월|(반)))?|(\d+)\s*개월)",
        text,
    )
    if not match:
        return None
    years, extra, half, single = match.groups()
    return int(years or 0) * 12 + int(extra or single or 0) + (6 if half else 0)


@dataclass
class Slots:
    """질문에서 규칙으로 뽑은 조건(상품 종류·금액·기간·점수·필터)과 라우팅 신호."""

    kinds: list[str] = field(default_factory=list)
    group: str | None = None
    amount_won: int | None = None
    monthly: bool = False
    term_months: int | None = None
    credit_score: int | None = None
    rate_pct: float | None = None
    banks: list[str] = field(default_factory=list)
    product_ids: list[str] = field(default_factory=list)
    metric: str | None = None
    filters: dict = field(default_factory=dict)
    compare: bool = False
    calc: bool = False
    goal: bool = False

    def to_dict(self) -> dict:
        """dict로 바꾼다(그래프 상태·세션 저장용)."""
        return asdict(self)


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
    rate = re.search(r"(\d+(?:\.\d+)?)\s*%", q)
    group = (
        "savingsbank"
        if "저축은행" in q
        else ("bank" if re.search(r"(^|\s)은행", q) else None)
    )
    metric = None
    if any(k in ("saving", "deposit") for k in kinds):
        metric = (
            "spread"
            if re.search(SPREAD_RE, q)
            else (
                "rate"
                if "기본" in q
                else ("rate_max" if re.search(r"최고|우대", q) else None)
            )
        )
    elif re.search(r"최고\s*금리|최대\s*금리", q):
        metric = "max"
    elif re.search(r"최저\s*금리", q):
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


def route_rules(q: str, s: Slots) -> tuple[str | None, str]:
    """(route, 근거). 규칙으로 못 정하면 (None, ...) -> LLM 분류로 넘긴다."""
    if re.search(OUT_OF_SCOPE_RE, q):
        return "out_of_scope", "금융상품 공시 범위 밖 주제"
    if not (s.kinds or s.product_ids or re.search(FINANCE_RE, q)):
        return "chat", "금융상품 관련 표현 없음"
    if s.compare:
        return "hard", "비교·집계 질문"
    if s.calc or s.goal or (s.amount_won and s.term_months):
        return "hard", "금액·기간이 있는 계산 질문"
    if s.product_ids or s.kinds:
        return "easy", "단일 상품·사실 조회"
    return None, "규칙으로 판단 불가"
