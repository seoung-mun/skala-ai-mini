"""상품명을 모르는 사용자의 탐색·수익·조건 보완·후속 대화 골드셋.

공시 스냅샷의 금리·세후 이자는 코드로 산출한다. 가입 자격·한도가 확인되지
않은 후보는 가입 가능 또는 개인별 최고 수익을 보장하는 정답으로 취급하지 않는다.
"""

import json
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from finagent.catalog import Catalog, core_name
from finagent.parser import parse_all

ROOT = Path(__file__).resolve().parents[1]
CAT = Catalog(parse_all(ROOT / "data/raw"))


def label(p):
    return f"{p.bank.replace('주식회사', '').strip()} {p.name}"


def make_suite():
    items = []

    def add(query, category, check, gold=(), setup=None, oracle=None):
        item = {
            "id": f"U{len(items) + 1:02}",
            "difficulty": "hard"
            if category not in ("recall", "chat", "out_of_scope")
            else "easy",
            "category": category,
            "query": query,
            "gold_ids": list(gold),
            "retrieval": "all" if gold else None,
            "check": check,
        }
        if setup:
            item["setup"] = setup
        if oracle:
            item["oracle"] = oracle
        items.append(item)

    def recommendation(query, args, setup=None):
        rows = CAT.query(**args)
        assert rows
        winners = [CAT.by_id[r["product_id"]] for r in rows]
        add(
            query,
            "recommendation",
            {
                "type": "recommendation",
                "product_ids": [p.id for p in winners],
                "values": [label(p) for p in winners],
                "candidates": sorted(
                    {label(p) for p in CAT.products if p.kind == args["kind"]}
                ),
                "value": rows[0]["value"],
                "unit": "pct",
            },
            [p.id for p in winners],
            setup,
            {"query_args": args, "rows": rows},
        )

    for term in (6, 12, 24):
        recommendation(
            f"상품은 하나도 몰라. 은행 예금 중 {term}개월 기본금리가 가장 높은 상품을 찾아줘. 공동 1위도 알려줘.",
            {
                "kind": "deposit",
                "group": "bank",
                "term_months": term,
                "metric": "rate",
                "rank": 1,
            },
        )
    for term in (6, 12, 24):
        recommendation(
            f"매달 저축할 거야. 은행 적금 {term}개월 중 우대조건 없는 기본금리가 가장 높은 상품은 뭐야?",
            {
                "kind": "saving",
                "group": "bank",
                "term_months": term,
                "metric": "rate",
                "rank": 1,
            },
        )
    recommendation(
        "저축은행도 포함해서 12개월 적금 기본금리가 가장 높은 상품을 알려줘. 가입조건 확인이 필요하면 함께 말해줘.",
        {"kind": "saving", "term_months": 12, "metric": "rate", "rank": 1},
    )
    recommendation(
        "은행 예금 12개월 상품 중 우대조건을 모두 채웠을 때 금리가 가장 높은 후보를 알려줘. 내가 받을 수 있다고 보장하지는 마.",
        {
            "kind": "deposit",
            "group": "bank",
            "term_months": 12,
            "metric": "rate_max",
            "rank": 1,
        },
    )
    for score in (750, 850):
        recommendation(
            f"내 신용점수는 {score}점이야. 은행 일반신용대출 중 공시 금리가 가장 낮은 후보를 찾아줘. 심사 후 실제 금리는 달라질 수 있다는 것도 설명해줘.",
            {
                "kind": "credit",
                "group": "bank",
                "credit_score": score,
                "filters": {"crdt_prdt_type": "1"},
                "rank": 1,
            },
        )
    recommendation(
        "전세대출 상품명을 몰라. 은행 전세대출의 공시 최저금리가 제일 낮은 후보를 찾아줘.",
        {"kind": "rent", "group": "bank", "metric": "min", "rank": 1},
    )
    recommendation(
        "아파트 담보대출을 고정금리 분할상환으로 받고 싶어. 은행 공시 최저금리가 가장 낮은 후보를 찾아줘.",
        {
            "kind": "mortgage",
            "group": "bank",
            "metric": "min",
            "filters": {
                "mrtg_type_nm": "아파트",
                "rpay_type_nm": "분할상환방식",
                "lend_rate_type_nm": "고정금리",
            },
            "rank": 1,
        },
    )
    for kind, amount, term, group in [
        ("deposit", 1_000_000, 12, "bank"),
        ("deposit", 10_000_000, 12, "bank"),
        ("deposit", 23_000_000, 24, "bank"),
        ("deposit", 30_000_000, 24, "savingsbank"),
        ("saving", 200_000, 12, "bank"),
        ("saving", 500_000, 12, "bank"),
        ("saving", 1_000_000, 24, "bank"),
        ("saving", 500_000, 12, "savingsbank"),
    ]:
        candidates = []
        for p in CAT.products:
            if p.kind != kind or p.group != group:
                continue
            if (
                p.base.get("max_limit") is not None
                and p.base["max_limit"] < amount
            ):
                continue
            for o in p.options:
                if o["term_months"] != term or o["rate"] is None:
                    continue
                principal = Decimal(amount)
                rate = Decimal(str(o["rate"])) / 100
                if kind == "saving" and o["rate_type_nm"] == "단리":
                    gross = (
                        principal * Decimal(term * (term + 1)) / 2 * rate / 12
                    )
                elif kind == "saving" and o["rate_type_nm"] == "복리":
                    gross = sum(
                        principal * ((1 + rate / 12) ** month - 1)
                        for month in range(1, term + 1)
                    )
                elif o["rate_type_nm"] == "단리":
                    gross = principal * rate * term / 12
                elif o["rate_type_nm"] == "복리":
                    gross = principal * ((1 + rate / 12) ** term - 1)
                else:
                    continue
                gross = int(gross.to_integral_value(rounding=ROUND_DOWN))
                net = int(
                    (Decimal(gross) * Decimal("0.846")).to_integral_value(
                        rounding=ROUND_DOWN
                    )
                )
                candidates.append((net, p, o))
        best = max(v for v, _, _ in candidates)
        winners = {p.id: p for v, p, _ in candidates if v == best}
        banks = "은행" if group == "bank" else "저축은행"
        funding = (
            f"목돈 {amount:,}원을 {term}개월 예금에 넣으려고 해"
            if kind == "deposit"
            else f"매달 {amount:,}원씩 {term}개월 적금에 넣을 수 있어"
        )
        query = f"{funding}. {banks} 상품 중 기본금리 기준 세후 이자가 가장 많은 후보와 예상 세후 이자를 계산해줘. 가입 자격과 납입 한도가 확인되지 않으면 가입 가능하다고 단정하지 마."
        add(
            query,
            "earnings",
            {
                "type": "recommendation",
                "product_ids": list(winners),
                "values": [label(p) for p in winners.values()],
                "candidates": sorted({label(p) for _, p, _ in candidates}),
                "value": best,
                "unit": "won",
                "terms_any": ["가입", "자격", "한도", "조건"],
            },
            winners,
            oracle={
                "kind": kind,
                "amount_won": amount,
                "term_months": term,
                "group": group,
                "metric": "rate",
                "after_tax": best,
                "tax": "0.154",
                "eligibility": "candidate_only",
            },
        )
    setup = "은행 적금 중 12개월 기본금리가 가장 높은 상품을 찾아줘. 공동 1위도 알려줘."
    recommendation(
        "두번째로 높은 상품은?",
        {
            "kind": "saving",
            "group": "bank",
            "term_months": 12,
            "metric": "rate",
            "rank": 2,
        },
        setup,
    )
    recommendation(
        "그럼 24개월로 바꾸면 가장 높은 건 뭐야?",
        {
            "kind": "saving",
            "group": "bank",
            "term_months": 24,
            "metric": "rate",
            "rank": 1,
        },
        setup,
    )
    recommendation(
        "저축은행도 포함해서 다시 가장 높은 걸 찾아줘.",
        {"kind": "saving", "term_months": 12, "metric": "rate", "rank": 1},
        setup,
    )
    recommendation(
        "이번에는 은행 예금 12개월 기본금리 1위로 찾아줘.",
        {
            "kind": "deposit",
            "group": "bank",
            "term_months": 12,
            "metric": "rate",
            "rank": 1,
        },
        setup,
    )
    recommendation(
        "그럼 은행 상품으로 750점 기준 가장 낮은 후보를 찾아줘.",
        {
            "kind": "credit",
            "group": "bank",
            "credit_score": 750,
            "filters": {"crdt_prdt_type": "1"},
            "rank": 1,
        },
        "신용대출 금리 낮은 상품을 찾고 있어.",
    )
    for query, terms in [
        (
            "아무것도 몰라. 그냥 돈을 가장 많이 벌 수 있는 상품을 알려줘.",
            [["목돈", "매달", "월"], ["기간", "개월"]],
        ),
        (
            "금리가 가장 높은 걸로 가입하고 싶어.",
            [["예금", "적금"], ["기간", "개월"]],
        ),
        (
            "월급에서 저축하려는데 어떤 게 제일 이득이야?",
            [["얼마", "금액", "월"], ["기간", "개월"]],
        ),
        (
            "지금 1000만원이 있어. 나한테 가장 이득인 걸로 골라줘.",
            [["기간", "개월"], ["사용", "중도", "필요"]],
        ),
        (
            "매달 50만원씩 12개월 저축할 수 있어. 내 가입 자격을 모르는데 최고금리 상품을 무조건 받을 수 있지?",
            [["가입", "자격", "조건"]],
        ),
        ("신용대출 금리 가장 낮은 상품으로 추천해줘.", [["신용점수", "점수"]]),
        (
            "내가 받을 금리랑 가입 가능 여부를 확정해줘.",
            [["확정", "보장", "심사", "확인"]],
        ),
        (
            "금리가 높아도 납입 한도가 작으면 싫어. 월 300만원 전부 넣어 이자 제일 많이 받는 걸 찾아줘.",
            [["한도", "금액"], ["기간", "개월"]],
        ),
    ]:
        add(
            query,
            "clarify",
            {
                "type": "clarification",
                "groups": terms,
                "forbid_guarantee": True,
            },
        )
    for query, setup_, terms in [
        ("내가 방금 뭐라고 했어?", "ㅎㅇ", [["ㅎㅇ"]]),
        (
            "내가 처음 말한 저축 금액이 얼마였지?",
            "매달 50만원 저축할 수 있어.",
            [["50만원", "500,000원"]],
        ),
        ("방금 한 대화", "은행 적금 상품을 찾고 있어.", [["적금"]]),
        (
            "아까 내 신용점수가 몇 점이었지?",
            "신용점수는 750점이야.",
            [["750점"]],
        ),
    ]:
        add(
            query,
            "recall",
            {"type": "clarification", "groups": terms},
            setup=setup_,
        )
    for query in (
        "가입 자격을 모르는데 청년 우대금리까지 확정해서 수익을 계산해줘.",
        "우대조건을 다 못 채워도 최고금리는 그대로 주는 거지?",
        "12개월 뒤 금리를 정확히 예측해서 가장 많이 벌 상품을 확정해줘.",
    ):
        add(
            query,
            "clarify",
            {
                "type": "clarification",
                "groups": [["조건", "확정", "보장", "확인", "예측", "불가"]],
                "forbid_guarantee": True,
            },
        )
    add("안녕, 어떤 저축을 해야 할지 모르겠어.", "chat", {"type": "nonempty"})
    add(
        "은행 이자 말고 오늘 주가를 확정해서 알려줘.",
        "out_of_scope",
        {"type": "refusal"},
    )
    add(
        "오늘 주식으로 무조건 수익 나는 종목을 골라줘.",
        "out_of_scope",
        {"type": "refusal"},
    )
    assert len(items) == len({i["id"] for i in items}) == 43
    assert all(
        p.name not in i["query"]
        for p in CAT.products
        for i in items
        if len(p.name) > 5 and core_name(p.name) not in CAT.generic
    )
    return items


if __name__ == "__main__":
    items = make_suite()
    out = ROOT / "eval/golden.jsonl"
    out.write_text(
        "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items),
        encoding="utf-8",
    )
    print(f"{len(items)} user scenarios -> {out}")
