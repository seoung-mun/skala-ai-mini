"""사용자 재현 입력: 금액·기간 경계와 금융 수치 표기 우회를 확인한다."""

import pytest

from finagent.slots import months, won_amounts
from finagent.validate import check_answer, unit_values


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("주담대 3억 30년 4.5%면", [300_000_000]),
        ("1억 5천 30년", [150_000_000]),
        ("1억5천", [150_000_000]),
        ("1억5천원", [100_005_000]),
        ("1억 2천만원", [120_000_000]),
        ("5천만원", [50_000_000]),
        ("2천5백만원", [25_000_000]),
        ("3.5억원", [350_000_000]),
        ("월 50만원, 30년, 연 4.5%", [500_000]),
        ("2700만원과 30,000원", [27_000_000, 30_000]),
        ("12개월 4.5퍼센트", []),
    ],
)
def test_money_boundaries(text, expected):
    assert won_amounts(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1년 6개월", 18),
        ("3년6개월", 42),
        ("2년 반", 30),
        ("2026년 9월 공시 기준 적금 12개월", 12),
        ("2026년 9월 1일 기준 1년 6개월", 18),
        ("2026년 9월 공시 기준", None),
        ("주담대 3억 30년 4.5%면", 360),
        ("6개월 또는 1년", 6),
    ],
)
def test_term_boundaries(text, expected):
    assert months(text) == expected


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("5천만원", "won", 50_000_000),
        ("1억 2천만원", "won", 120_000_000),
        ("9.99퍼센트", "pct", 9.99),
        ("0.7퍼센트 포인트", "pct", 0.7),
        ("3.5억원", "won", 350_000_000),
        ("-5천만원", "won", -50_000_000),
    ],
)
def test_financial_spelling_cannot_bypass_validation(text, kind, value):
    assert unit_values(text) == [(0, kind, value)]
    assert not check_answer(text, {"pct": {3.94}, "won": {27_000_000}}, [], {})[
        0
    ]
    assert check_answer(
        text,
        {
            "pct": {value} if kind == "pct" else set(),
            "won": {value} if kind == "won" else set(),
        },
        [],
        {},
    )[0]


def test_compound_money_is_one_claim_and_values_remain_in_order():
    assert unit_values("원금 1억 2천만원, 금리 3.94퍼센트") == [
        (3, "won", 120_000_000),
        (15, "pct", 3.94),
    ]


def test_percentage_precision_is_not_rounded_into_allowed_value():
    assert not check_answer(
        "9.994퍼센트", {"pct": {9.99}, "won": set()}, [], {}
    )[0]


def test_fractional_won_input_is_not_silently_truncated():
    with pytest.raises(ValueError, match="원 미만"):
        won_amounts("1.5원")


def test_recall_false_positive_still_reaches_financial_route():
    import asyncio
    from types import SimpleNamespace

    from test_graph import CAT, FakeLLM

    from finagent.graph.nodes import route_node

    result = asyncio.run(
        route_node(
            {"query": "마지막으로 금리 비교 요청 어떤 게 좋아?"},
            SimpleNamespace(catalog=CAT, llm=FakeLLM()),
        )
    )
    assert result["route"] == "hard"
    assert result["intent"] != "recall"


def test_amount_term_and_rate_are_extracted_independently():
    from legacy_rules import extract
    from test_graph import CAT

    slots = extract("주담대 3억 30년 4.5%면 월 상환액 얼마야?", CAT)
    assert slots.amount_won == 300_000_000
    assert slots.term_months == 360
    assert slots.rate_pct == 4.5


def test_non_currency_counters_do_not_become_allowed_money():
    assert won_amounts("가입자 1만명, 상품 2만개, 예치금 3억원") == [
        300_000_000
    ]
    assert unit_values("가입자 1만명, 상품 2만개") == []
