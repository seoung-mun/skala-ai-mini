"""재작성·계획이 사용자 조건을 바꾸지 못하는지 실제 카탈로그 경계에서 확인한다."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from legacy_rules import extract
from test_graph import FakeLLM, route_reply

from finagent.catalog import Catalog
from finagent.graph.nodes import (
    gate_node,
    plan_gate,
    plan_node,
    replan_check_node,
    route_node,
)
from finagent.parser import parse_all
from finagent.schemas import PlanOutput

CAT = Catalog(parse_all(Path(__file__).resolve().parents[1] / "data/raw"))


def test_empty_query_keeps_explicit_period_and_returns_no_matches():
    query = "은행 13개월 정기예금 기본금리 높은 순으로 비교해줘"
    args = {"kind": "deposit", "group": "bank", "term_months": 13}
    assert CAT.query(**args) == []
    task = {"id": "t1", "agent": "structured", "args": args, "round": 0}
    state = {
        "query": query,
        "route": "hard",
        "tasks": [task],
        "results": [{"task_id": "t1", "round": 0, "ok": True, "rows": []}],
    }
    delta = asyncio.run(replan_check_node(state, None))
    assert delta.get("tasks", [task])[0]["args"] == args
    assert delta["events"][0]["is_sufficient"] is False
    assert "조회" in delta["events"][0]["reason"]


def test_calculation_followup_preserves_amount_and_intent_without_model_rewrite():
    previous = "우리SUPER주거래적금 월 50만원씩 12개월 세후 이자 얼마야?"
    state = {
        "query": "그럼 24개월은?",
        "prev_slots": extract(previous, CAT).to_dict(),
        "history": [
            {"role": "user", "content": previous},
            {"role": "assistant", "content": "세후 이자 67,362원입니다."},
        ],
    }
    result = asyncio.run(
        route_node(state, SimpleNamespace(catalog=CAT, llm=FakeLLM()))
    )
    assert result["route"] == "hard"
    assert result["slots"]["amount_won"] == 500000
    assert result["slots"]["term_months"] == 24
    assert result["slots"]["monthly"] is True
    assert result["slots"]["question"] == state["query"]
    assert "rewrite" not in result["events"][0]


def test_planner_cannot_change_deposit_to_saving_calculation():
    slots = extract(
        "정기예금에 1000만원 12개월 연 3% 넣으면 이자 계산해줘", CAT
    )
    plan = PlanOutput(
        ok=True,
        tasks=[{"agent": "calc", "kind": "saving", "why": "오류"}],
        reason="오류",
    )
    kept, removed = plan_gate(plan, slots)
    assert not kept and removed


def test_lossy_model_calculation_is_rejected_instead_of_silently_repaired():
    import pytest
    from test_llm_routing import RoutedLLM

    from finagent.schemas import LLMOutputError

    query = "그 상품에 월 50만원씩 12개월 넣으면 세후 이자가 얼마야?"
    llm = RoutedLLM(
        route_reply(
            "calculate",
            kinds=["saving"],
            products=["우리SUPER주거래적금"],
            term_months=12,
            monthly=True,
            calculation="forward",
        )
    )
    with pytest.raises(LLMOutputError, match="계산 금액이 누락"):
        asyncio.run(
            route_node({"query": query}, SimpleNamespace(catalog=CAT, llm=llm))
        )


def test_spread_lookup_is_not_reused_as_calculation_rate():
    class Prompts:
        async def get_prompt(self, name, arguments):
            return "계산"

    query = "카카오뱅크 자유적금에 매월 20만원씩 12개월 넣으면 우대 폭은 얼마고 이자는 얼마야?"
    deps = SimpleNamespace(catalog=CAT, planner="rule", mcp=Prompts())
    delta = asyncio.run(
        plan_node(
            {"query": query, "slots": extract(query, CAT).to_dict()}, deps
        )
    )
    calc = next(t for t in delta["tasks"] if t["agent"] == "calc")
    assert calc["rate_query"]["metric"] == "rate"


def test_monthly_interest_question_does_not_change_deposit_to_saving():
    class Prompts:
        async def get_prompt(self, name, arguments):
            return "계산"

    query = "정기예금에 1000만원을 12개월 연 3%로 넣으면 매월 이자는 얼마야?"
    deps = SimpleNamespace(catalog=CAT, planner="rule", mcp=Prompts())
    result = asyncio.run(
        plan_node(
            {"query": query, "slots": extract(query, CAT).to_dict()}, deps
        )
    )
    calc = next(t for t in result["tasks"] if t["agent"] == "calc")
    assert calc["args"]["product_type"] == "deposit"
    slots = {
        "monthly_payout": True
    }  # 라우터 LLM이 월별 지급 이자 요청으로 해석한 결과
    gated = asyncio.run(
        gate_node({"query": query, "tasks": [calc], "slots": slots}, deps)
    )
    assert not gated["tasks"]
    assert "월별 지급 이자" in gated["clarify"]
