"""파싱된 상품 위의 구조화 조회·계산 (순수 함수). MCP 서버와 테스트가 같은 함수를 쓴다."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .parser import GRADES, Product

# 사용자 표현 -> 금융회사명(공시명). 공시명에 '주식회사'가 붙은 곳이 많아 별칭이 필요하다.
BANK_ALIASES = {
    "SC제일": "한국스탠다드차타드은행",
    "IBK": "중소기업은행",
    "기업은행": "중소기업은행",
    "농협": "농협은행주식회사",
    "NH": "농협은행주식회사",
    "KB": "국민은행",
    "iM뱅크": "아이엠뱅크",
    "대구은행": "아이엠뱅크",
    "산업은행": "한국산업은행",
    "KDB": "한국산업은행",
    "SH": "수협은행",
    "수협": "수협은행",
    "카카오": "주식회사 카카오뱅크",
    "케이뱅크": "주식회사 케이뱅크",
    "토스": "토스뱅크 주식회사",
    "하나": "주식회사 하나은행",
}
LOAN_KINDS = ("mortgage", "rent", "credit")
# 순위에 쓸 수 있는 금리. 공시 평균금리(avg)는 범위 밖 값이 많아 허용하지 않는다. 신용대출은 점수 구간 금리만 쓴다.
METRICS = {
    "saving": ("rate", "rate_max", "spread"),
    "deposit": ("rate", "rate_max", "spread"),
    "mortgage": ("min", "max"),
    "rent": ("min", "max"),
    "credit": (),
}
# 이름이 이 분류어뿐인 상품('신용대출', 'e-정기예금')은 질문 속 분류어와 구분이 안 돼 은행명 없이 매칭하지 않는다.
CATEGORY_RE = (
    r"일반신용대출|마이너스한도대출|장기카드대출|신용대출|한도대출|카드론|주택담보대출|담보대출|전세자금대출|"
    r"전세대출|자금대출|대출|정기예금|정기적금|자유적금|적금|예금|통장|상품|개인|일반"
)
CREDIT_TYPE = {"일반": "1", "마이너스": "2", "카드론": "3"}
# 종류별로 쓸 수 있는 옵션 필터 키. 이 밖의 키는 조회에서 오류다(조용히 무시하지 않는다).
FILTER_KEYS = {
    "saving": ("rsrv_type_nm",),
    "deposit": (),
    "mortgage": ("mrtg_type_nm", "rpay_type_nm", "lend_rate_type_nm"),
    "rent": ("rpay_type_nm", "lend_rate_type_nm"),
    "credit": ("crdt_prdt_type",),
}


def filters_for(kind: str, filters: dict) -> dict:
    """슬롯 필터 중 해당 종류에 적용되는 키만 남긴다. 호출 쪽이 쓰고, 조회는 남은 키를 엄격히 검사한다."""
    return {k: v for k, v in filters.items() if k in FILTER_KEYS[kind]}


def squash(s: str) -> str:
    """공백을 모두 지우고 소문자로 바꾼다(이름 비교용)."""
    return re.sub(r"\s+", "", s).lower()


def core_name(name: str) -> str:
    """'IBK D-day적금(자유적립식)' -> 'ibkd-day적금' (괄호 부연은 사용자가 잘 안 쓴다)."""
    return squash(re.sub(r"\([^)]*\)", "", name))


@dataclass
class Catalog:
    """파싱된 상품 목록 위의 이름 매칭과 구조화 금리 조회."""

    products: list[Product]

    def __post_init__(self):
        """id 색인·일반명 집합·금융회사 목록을 만든다. 상품 id가 중복되면 ValueError."""
        self.by_id = {p.id: p for p in self.products}
        if len(self.by_id) != len(self.products):
            raise ValueError("상품 id 중복")
        banks_per_name: dict[str, set[str]] = {}
        for p in self.products:
            banks_per_name.setdefault(core_name(p.name), set()).add(p.bank)
        # '정기예금'처럼 여러 회사가 같은 이름을 쓰면 이름만으로는 상품을 특정할 수 없다.
        self.generic = {
            n
            for n, b in banks_per_name.items()
            if len(b) > 1
            or len(re.sub(r"[^가-힣a-z0-9]", "", re.sub(CATEGORY_RE, "", n)))
            < 2
        }
        self.banks = sorted({p.bank for p in self.products})

    # ---------- 이름 매칭 ----------
    def find_banks(self, text: str) -> list[str]:
        """텍스트에 나온 금융회사(공시명·별칭)를 공시명 목록으로 돌려준다."""
        t = squash(text)
        hits = {b for b in self.banks if squash(b.replace("주식회사", "")) in t}
        hits |= {
            full
            for alias, full in BANK_ALIASES.items()
            if squash(alias) in t and full in self.banks
        }
        return sorted(hits)

    def find_products(self, text: str) -> list[Product]:
        """텍스트에 이름이 나온 상품을 찾는다.
        일반명 상품은 은행명이 함께 있을 때만 고르고, 더 긴 이름에 포함된 짧은 매칭은 버린다."""
        t, banks = squash(text), set(self.find_banks(text))
        cand = [
            p
            for p in self.products
            if core_name(p.name)
            and (squash(p.name) in t or core_name(p.name) in t)
        ]
        exact = [p for p in cand if squash(p.name) in t]
        if exact:  # '신한주택대출(아파트)'처럼 전체 이름이 있으면 괄호만 다른 상품은 뺀다
            cand = exact
        if banks:
            cand = [p for p in cand if p.bank in banks] or [
                p for p in cand if core_name(p.name) not in self.generic
            ]
        else:
            cand = [p for p in cand if core_name(p.name) not in self.generic]
        names = {squash(p.name) for p in cand}
        # 더 긴 이름에 포함되는 짧은 이름 매칭은 버린다 ('26주적금' < '카카오뱅크26주적금')
        return [
            p
            for p in cand
            if not any(
                n != squash(p.name) and squash(p.name) in n for n in names
            )
        ]

    # ---------- 구조화 조회 ----------
    def query(
        self,
        kind: str,
        *,
        group: str | None = None,
        term_months: int | None = None,
        metric: str | None = None,
        credit_score: int | None = None,
        product_ids: list[str] | None = None,
        banks: list[str] | None = None,
        filters: dict | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """상품별 최적 옵션 1행씩 정렬해 반환. 예·적금은 높은 금리, 대출은 낮은 금리가 앞. 동률은 모두 포함."""
        if kind not in METRICS:
            raise ValueError(f"알 수 없는 상품 종류: {kind}")
        if metric is not None and metric not in METRICS[kind]:
            raise ValueError(
                f"{kind}의 metric은 {list(METRICS[kind]) or '지정 불가(신용점수 구간 금리)'} 중 하나여야 합니다"
                f": {metric} (공시 평균금리는 순위에 쓰지 않습니다)"
            )
        metric = metric or default_metric(kind)
        if kind == "credit" and credit_score is None:
            raise ValueError("신용대출 금리 조회에는 신용점수가 필요합니다")
        if limit < 1:
            raise ValueError(f"limit은 1 이상이어야 합니다: {limit}")
        if credit_score is not None and kind != "credit":
            raise ValueError(
                f"credit_score는 신용대출에만 적용됩니다({kind}에는 넣지 마세요)"
            )
        if term_months is not None and kind in LOAN_KINDS:
            raise ValueError(
                f"term_months는 예·적금에만 적용됩니다({kind}에는 넣지 마세요)"
            )
        if bad := sorted(set(filters or {}) - set(FILTER_KEYS[kind])):
            raise ValueError(
                f"{kind}에 쓸 수 없는 filters 키 {bad}. 허용: {list(FILTER_KEYS[kind]) or '없음'}"
            )
        if bad := [b for b in banks or [] if b not in self.banks]:
            raise ValueError(
                f"공시에 없는 금융회사명 {bad}. 공시명 그대로 넣으세요(예: '주식회사 카카오뱅크')"
            )
        if bad := [
            i
            for i in product_ids or []
            if i not in self.by_id or self.by_id[i].kind != kind
        ]:
            raise ValueError(f"{kind} 상품이 아닌 product_id {bad}")
        grade = score_band(credit_score)[0] if kind == "credit" else None
        rows = []
        for p in self.products:
            if (
                p.kind != kind
                or (group and p.group != group)
                or (banks and p.bank not in banks)
                or (product_ids and p.id not in product_ids)
            ):
                continue
            if (
                kind == "credit"
                and (filters or {}).get("crdt_prdt_type", "1")
                != p.base["crdt_prdt_type"]
            ):
                continue
            best = None
            for o in p.options:
                if not _option_ok(kind, o, term_months, filters or {}):
                    continue
                v = (
                    o["grades"][grade]
                    if kind == "credit" and o["type"] == "A"
                    else (None if kind == "credit" else o.get(metric))
                )
                if v is None:
                    continue
                if best is None or (
                    v < best[0] if kind in LOAN_KINDS else v > best[0]
                ):
                    best = (v, o)
            if best:
                rows.append(
                    {
                        "product_id": p.id,
                        "bank": p.bank,
                        "product": p.name,
                        "group": p.group,
                        "kind": kind,
                        "metric": metric
                        if kind != "credit"
                        else f"대출금리@{grade}",
                        "value": best[0],
                        "option": _option_label(kind, best[1], grade),
                    }
                )
        rows.sort(
            key=lambda r: (
                r["value"] if kind in LOAN_KINDS else -r["value"],
                r["product_id"],
            )
        )
        if len(rows) > limit:  # 경계 동률은 잘라내지 않는다
            cut = rows[limit - 1]["value"]
            rows = rows[:limit] + [r for r in rows[limit:] if r["value"] == cut]
        return rows


def default_metric(kind: str) -> str:
    """종류별 기본 순위 금리: 예·적금은 기본금리(rate), 대출은 최저금리(min)."""
    return "rate" if kind in ("saving", "deposit") else "min"


def _option_ok(kind: str, o: dict, term: int | None, f: dict) -> bool:
    """옵션 행이 기간·적립방식·대출 옵션 필터를 모두 만족하는지 본다."""
    if (
        kind in ("saving", "deposit")
        and term is not None
        and o["term_months"] != term
    ):
        return False
    if (
        kind == "saving"
        and f.get("rsrv_type_nm")
        and o["rsrv_type_nm"] != f["rsrv_type_nm"]
    ):
        return False
    return all(
        o.get(k) == v
        for k, v in f.items()
        if k in ("mrtg_type_nm", "rpay_type_nm", "lend_rate_type_nm")
    )


def _option_label(kind: str, o: dict, grade: str | None) -> str:
    """조회 결과에 보여줄 옵션 설명(기간·방식, 신용대출은 점수 구간)."""
    if kind in ("saving", "deposit"):
        return " ".join(
            filter(
                None,
                (
                    f"{o['term_months']}개월",
                    o.get("rsrv_type_nm"),
                    o["rate_type_nm"],
                ),
            )
        )
    if kind == "credit":
        return dict(GRADES)[grade]
    return " ".join(
        filter(
            None,
            (o.get("mrtg_type_nm"), o["rpay_type_nm"], o["lend_rate_type_nm"]),
        )
    )


# ---------- 계산 ----------
def score_band(score: int) -> tuple[str, str]:
    """신용점수(0~1000)를 공시 점수 구간 (코드, 라벨)로 바꾼다. 범위 밖이면 ValueError."""
    if not 0 <= score <= 1000:
        raise ValueError(f"신용점수는 0~1000이어야 합니다: {score}")
    for limit, code in (
        (900, "crdt_grad_1"),
        (800, "crdt_grad_4"),
        (700, "crdt_grad_5"),
        (600, "crdt_grad_6"),
        (500, "crdt_grad_10"),
        (400, "crdt_grad_11"),
        (300, "crdt_grad_12"),
    ):
        if score > limit:
            return code, dict(GRADES)[code]
    return "crdt_grad_13", dict(GRADES)["crdt_grad_13"]


def _check(**kw):
    """계산 인자가 모두 0보다 큰지 검사한다. 아니면 ValueError."""
    for k, v in kw.items():
        if v is None or v <= 0:
            raise ValueError(f"{k}는 0보다 커야 합니다: {v}")


def saving_interest(
    monthly_won: int, term_months: int, rate_pct: float, compound: bool = False
) -> int:
    """적금 세전이자(원, 원 미만 절사). 단리: 월납입 × T(T+1)/2 × 연이율/1200. 복리: 월복리 누적."""
    _check(monthly_won=monthly_won, term_months=term_months, rate_pct=rate_pct)
    if compound:
        r = rate_pct / 1200
        return math.floor(
            sum(
                monthly_won * ((1 + r) ** k - 1)
                for k in range(1, term_months + 1)
            )
        )
    return math.floor(
        monthly_won * term_months * (term_months + 1) / 2 * rate_pct / 1200
    )


def deposit_interest(
    principal_won: int,
    term_months: int,
    rate_pct: float,
    compound: bool = False,
) -> int:
    """정기예금 세전이자(원, 원 미만 절사). 단리: 원금 × 연이율 × T/1200. 복리: 월복리."""
    _check(
        principal_won=principal_won, term_months=term_months, rate_pct=rate_pct
    )
    if compound:
        return math.floor(
            principal_won * ((1 + rate_pct / 1200) ** term_months - 1)
        )
    return math.floor(principal_won * rate_pct * term_months / 1200)


def loan_payment(
    principal_won: int,
    term_months: int,
    rate_pct: float,
    method: str = "equal_payment",
) -> int:
    """월 상환액(원, 반올림). equal_payment=원리금균등, bullet=만기일시(월 이자만)."""
    _check(
        principal_won=principal_won, term_months=term_months, rate_pct=rate_pct
    )
    r = rate_pct / 1200
    if method == "bullet":
        return round(principal_won * r)
    if method != "equal_payment":
        raise ValueError(f"지원하지 않는 상환방식: {method}")
    return round(principal_won * r / (1 - (1 + r) ** -term_months))


# ---------- 역산(목표 → 필요 금액) ----------
INTEREST_TAX = 0.154  # 일반과세 이자소득세 15.4%(소득세 14% + 지방소득세 1.4%)


def after_tax(interest: int) -> int:
    """세전이자에서 이자소득세 15.4%를 뺀 세후이자(원 미만 절사)."""
    return int(interest * (1 - INTEREST_TAX))


def _least(ok, hi: int) -> int:
    """ok(x)가 참인 최소 정수 x (1 <= x <= hi, ok는 단조). 원 단위 절사가 있어 식을 뒤집지 않고 정방향 함수로 찾는다."""
    lo = 1
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if ok(mid) else (mid + 1, hi)
    return lo


def saving_goal(
    target_won: int, term_months: int, rate_pct: float, compound: bool = False
) -> int:
    """세후 만기 수령액(월납입 × T + 세후이자)이 목표 이상이 되는 최소 월 납입액(원)."""
    _check(target_won=target_won, term_months=term_months, rate_pct=rate_pct)
    return _least(
        lambda m: (
            m * term_months
            + after_tax(saving_interest(m, term_months, rate_pct, compound))
            >= target_won
        ),
        target_won,
    )


def deposit_goal(
    target_won: int, term_months: int, rate_pct: float, compound: bool = False
) -> int:
    """세후 만기 수령액(원금 + 세후이자)이 목표 이상이 되는 최소 원금(원)."""
    _check(target_won=target_won, term_months=term_months, rate_pct=rate_pct)
    return _least(
        lambda p: (
            p + after_tax(deposit_interest(p, term_months, rate_pct, compound))
            >= target_won
        ),
        target_won,
    )


def loan_capacity(
    monthly_budget_won: int,
    term_months: int,
    rate_pct: float,
    method: str = "equal_payment",
) -> int:
    """월 상환액이 예산 이하인 최대 대출 원금(원)."""
    _check(
        monthly_budget_won=monthly_budget_won,
        term_months=term_months,
        rate_pct=rate_pct,
    )
    # 원금이 이 값이면 월 상환액이 반드시 예산을 넘는다(원리금균등 >= 원금/T, 만기일시 = 원금 × r)
    hi = (
        (monthly_budget_won + 1) * term_months
        if method == "equal_payment"
        else math.ceil((monthly_budget_won + 1) * 1200 / rate_pct)
    )
    p = (
        _least(
            lambda x: (
                loan_payment(x, term_months, rate_pct, method)
                > monthly_budget_won
            ),
            hi,
        )
        - 1
    )
    if p <= 0:
        raise ValueError(
            f"월 상환 가능액 {monthly_budget_won}원으로는 대출 원금을 만들 수 없습니다"
        )
    return p
