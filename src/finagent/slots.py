"""질문에서 슬롯과 라우팅 신호를 규칙으로 뽑는다. LLM 없이 재현 가능해야 라우팅 정확도를 측정할 수 있다."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .catalog import BANK_ALIASES, CREDIT_TYPE, Catalog, squash

UNIT = {"조": 10**12, "억": 10**8, "천만": 10**7, "만": 10**4, "천": 10**3}
MONEY_RE = re.compile(r"(?:\d[\d,]*\s*(?:조|억|천만|만|천)\s*)+(?:\d[\d,]*\s*)?원?|\d[\d,]*\s*원")
KIND_RE = [("credit", r"신용대출|마이너스|신용점수|카드론"), ("rent", r"전세"),
           ("mortgage", r"주담대|주택담보|담보대출|아파트\s*담보|집\s*담보"), ("saving", r"적금"), ("deposit", r"예금")]
OUT_OF_SCOPE_RE = r"주식|주가|코인|비트코인|이더리움|보험|날씨|환율|펀드|로또|부동산\s*시세"
FINANCE_RE = r"금리|이자|우대|대출|예금|적금|은행|뱅크|가입|상품|수수료|한도|상환|신용|저축|빌리|빌려|갚"
COMPARE_RE = r"제일|가장|비교|중에|중\s|순서|순위|추천|어디|어느|뭐가\s*나아|더\s*높|더\s*낮|\d+\s*개\s*(보여|알려)"
CALC_RE = r"이자\s*(얼마|는)|얼마나\s*(돼|받|붙)|상환액|한\s*달에\s*얼마|월\s*얼마|갚으면|빌리면|빌려서|넣으면|세전이자|만기에\s*얼마"


def won_amounts(text: str) -> list[int]:
    out = []
    for m in MONEY_RE.finditer(text):
        parts = re.findall(r"(\d[\d,]*)\s*(조|억|천만|만|천)?", m.group(0))
        out.append(sum(int(n.replace(",", "")) * (UNIT[u] if u else 1) for n, u in parts))
    return out


def months(text: str) -> int | None:
    if m := re.search(r"(\d+)\s*년\s*(반)?", text):
        return int(m.group(1)) * 12 + (6 if m.group(2) else 0)
    if m := re.search(r"(\d+)\s*개월", text):
        return int(m.group(1))
    return None


@dataclass
class Slots:
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

    def to_dict(self) -> dict:
        return asdict(self)


def extract(q: str, catalog: Catalog) -> Slots:
    kinds = [k for k, pat in KIND_RE if re.search(pat, q)]
    prods = catalog.find_products(q)
    for p in prods:
        if p.kind not in kinds:
            kinds.append(p.kind)
    amounts = won_amounts(q)
    score = next((int(s) for s in re.findall(r"(\d{3,4})\s*점", q) if 0 <= int(s) <= 1000), None)
    rate = re.search(r"(\d+(?:\.\d+)?)\s*%", q)
    group = "savingsbank" if "저축은행" in q else ("bank" if re.search(r"(^|\s)은행", q) else None)
    metric = None
    if any(k in ("saving", "deposit") for k in kinds):
        metric = "rate" if "기본" in q else ("rate_max" if re.search(r"최고|우대", q) else None)
    elif re.search(r"최고\s*금리|최대\s*금리", q):
        metric = "max"
    f = {}
    for word, key, val in (("아파트외", "mrtg_type_nm", "아파트외"), ("분할", "rpay_type_nm", "분할상환방식"),
                           ("만기일시", "rpay_type_nm", "만기일시상환방식"), ("고정", "lend_rate_type_nm", "고정금리"),
                           ("변동", "lend_rate_type_nm", "변동금리"), ("정액", "rsrv_type_nm", "정액적립식"),
                           ("자유적립", "rsrv_type_nm", "자유적립식")):
        if word in q:
            f.setdefault(key, val)
    if "아파트" in q and "mrtg_type_nm" not in f:
        f["mrtg_type_nm"] = "아파트"
    if "credit" in kinds:
        f["crdt_prdt_type"] = next((v for w, v in CREDIT_TYPE.items() if w in q and w != "일반"), "1")
    term = months(q)
    # 'SC제일은행'의 '제일'처럼 이름 속 단어가 비교어로 잡히지 않게 이름을 지우고 신호를 찾는다.
    cue = q
    for name in [p.name for p in prods] + catalog.find_banks(q) + list(BANK_ALIASES):
        cue = cue.replace(name, " ")
    distinct = {squash(p.name) for p in prods}
    return Slots(kinds=kinds, group=group, amount_won=amounts[0] if amounts else None,
                 monthly=bool(re.search(r"(월|매달|매월)\s*\d", q)), term_months=term, credit_score=score,
                 rate_pct=float(rate.group(1)) if rate else None, banks=catalog.find_banks(q),
                 product_ids=[p.id for p in prods], metric=metric, filters=f,
                 compare=bool(re.search(COMPARE_RE, cue)) or len(distinct) >= 2,
                 calc=bool(re.search(CALC_RE, cue)) and bool(amounts))


def route_rules(q: str, s: Slots) -> tuple[str | None, str]:
    """(route, 근거). 규칙으로 못 정하면 (None, ...) -> LLM 분류로 넘긴다."""
    if re.search(OUT_OF_SCOPE_RE, q):
        return "out_of_scope", "금융상품 공시 범위 밖 주제"
    if not (s.kinds or s.product_ids or re.search(FINANCE_RE, q)):
        return "chat", "금융상품 관련 표현 없음"
    if s.compare:
        return "hard", "비교·집계 질문"
    if s.calc or (s.amount_won and s.term_months):
        return "hard", "금액·기간이 있는 계산 질문"
    if s.product_ids or s.kinds:
        return "easy", "단일 상품·사실 조회"
    return None, "규칙으로 판단 불가"
