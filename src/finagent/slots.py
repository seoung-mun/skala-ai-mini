"""LLM 조건을 실행 상태로 검증·변환한다. 규칙 추출기는 eval/legacy_rules.py(테스트·과거 평가 전용)에 있다."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .amounts import money_values
from .catalog import BANK_ALIASES, Catalog, core_name, squash

# "기본금리나 전체 우대 폭은 빼고"처럼 제외한 항목은 묻는 금리 종류로 읽지 않는다.
EXCLUDED_ITEM_RE = (
    r"(?:[^\s.?!,]+\s+){0,3}[^\s.?!,]+?(?:은|는|을|를)\s*(?:빼고|제외하고|말고)"
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
    """조회·계산에 사용할 검증된 조건."""

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
    rank: int | None = None
    exclude_ids: list[str] = field(default_factory=list)
    repayment: str | None = None
    monthly_payout: bool = False
    docs: bool = False
    count: int | None = None
    calc: bool = False
    goal: bool = False
    question: str = ""  # 검증된 독립 질문을 보존해 연속 후속 질문에서도 계산 의도를 유지한다.

    def to_dict(self) -> dict:
        """dict로 바꾼다(그래프 상태·세션 저장용)."""
        return asdict(self)


def from_route(
    decision, catalog: Catalog, question: str, history: list
) -> Slots:
    """LLM이 고른 조건만 변환한다. 질문의 단어로 은행·상품·경로를 다시 고르지 않는다."""
    c = decision.conditions
    canonical = {squash(b.replace("주식회사", "")): b for b in catalog.banks}
    canonical.update(
        {squash(a): b for a, b in BANK_ALIASES.items() if b in catalog.banks}
    )
    banks = []
    for name in c.banks:
        bank = canonical.get(squash(name.replace("주식회사", "")))
        if bank is None:
            raise ValueError(f"공시에 없는 금융회사: {name}")
        if bank not in banks:
            banks.append(bank)

    def full_name(p) -> str:
        return squash(p.bank.replace("주식회사", "") + p.name)

    wanted = {squash(name.replace("주식회사", "")) for name in c.products}
    ids = [
        p.id
        for p in catalog.products
        if (
            full_name(p)
            in wanted  # '조은저축은행 정기예금(서울본점)'처럼 은행명까지 쓴 이름
            or (
                any(
                    squash(name) == squash(p.name)
                    or core_name(name) == core_name(p.name)
                    for name in c.products
                )
                and (banks or core_name(p.name) not in catalog.generic)
            )
        )
        and (not banks or p.bank in banks)
        and (not c.kinds or p.kind in c.kinds)
        and (c.group is None or p.group == c.group)
        and (
            p.kind != "credit"
            or c.filters.crdt_prdt_type is None
            or p.base["crdt_prdt_type"] == c.filters.crdt_prdt_type
        )
    ]
    if ids:
        # '그 상품이랑 카카오뱅크 비교'처럼 상품과 금융회사를 함께 지정하면 상품이 없는 금융회사도 후보에 넣는다.
        covered = {catalog.by_id[i].bank for i in ids}
        ids += [
            p.id
            for p in catalog.products
            if p.bank in banks
            and p.bank not in covered
            and (not c.kinds or p.kind in c.kinds)
        ]
    s = Slots(
        **c.model_dump(
            exclude={
                "products",
                "banks",
                "filters",
                "calculation",
                "exclude_previous",
            }
        ),
        banks=banks,
        product_ids=ids,
        filters=c.filters.model_dump(exclude_none=True),
        calc=c.calculation != "none",
        goal=c.calculation == "goal",
        compare=decision.rank is not None
        or decision.comparison_scope != "none",
        rank=decision.rank,
        question=question,
    )
    if s.amount_won is not None and s.amount_won < 1_000:
        raise ValueError(
            f"계산 금액 {s.amount_won}원은 너무 작습니다. 단위를 생략한 저축 금액은 만원 단위입니다"
        )
    if s.amount_won is not None and not decision.clarify:
        # 의도는 LLM이 정하고, 계산 금액이 사용자가 쓴 숫자에서 나왔는지만 본다('월 50'처럼 단위를 생략해도 허용).
        said = "\n".join(
            [m["content"] for m in history if m["role"] == "user"] + [question]
        )
        numbers = {
            int(n.replace(",", ""))
            for n in re.findall(r"\d[\d,]*", said)
            if n.replace(",", "").isdigit()
        }
        units = (1, 1_000, 10_000, 1_000_000, 10_000_000, 100_000_000)
        if s.amount_won not in set(won_amounts(said)) | {
            n * u for n in numbers for u in units
        }:
            raise ValueError("계산 금액이 사용자 발화에 없습니다")
    if s.calc and s.amount_won is None and won_amounts(question):
        raise ValueError("사용자가 지정한 계산 금액이 누락됐습니다")
    if s.rate_pct is not None:
        from .validate import values_of

        user_text = "\n".join(
            [m["content"] for m in history if m["role"] == "user"] + [question]
        )
        if s.rate_pct not in values_of(user_text, "pct"):
            raise ValueError(
                "계산 연이율이 사용자 발화에 없습니다. 상품 금리는 도구로 조회해야 합니다"
            )
    if s.amount_won is not None and {"deposit", "saving"} <= set(s.kinds):
        # 목돈은 예금, 매달 납입은 적금에만 넣을 수 있다.
        s.kinds = [
            k for k in s.kinds if k != ("deposit" if s.monthly else "saving")
        ]
    return s
