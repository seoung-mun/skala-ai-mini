"""세션 경계·실제 SDK 입력·시장 순위의 회귀 검사. 실모델 정답률은 별도 검증한다."""

import asyncio
import json
from types import SimpleNamespace

import httpx
from langchain_openai import ChatOpenAI
from legacy_rules import extract
from test_graph import CAT, route_reply
from test_usage_failures import completion

from finagent.graph.nodes import route_node
from finagent.llm import OpenAILLM, session_context, session_messages
from finagent.schemas import ChatInput, ChatOutput, LLMOutputError


def test_all_turns_reach_sdk_without_cross_session_leakage():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json=completion('{"ok":true,"reply":"확인"}')
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        llm = OpenAILLM("gpt-4.1-mini", api_key="test")
        llm.client = ChatOpenAI(
            model="gpt-4.1-mini",
            api_key="test",
            http_client=client,
            max_retries=0,
        )

        async def one(label):
            history = [
                {"role": role, "content": f"{label}-{i}"}
                for i, role in enumerate(["user", "assistant"] * 5)
            ]
            with session_context(history):
                await asyncio.to_thread(
                    llm.structured, "sys", ChatInput(question=label), ChatOutput
                )

        async def both():
            await asyncio.gather(one("A"), one("B"))

        asyncio.run(both())
    assert not session_messages()
    assert len(sent) == 2
    for request in sent:
        label = request["messages"][-1]["content"].splitlines()[-1]
        assert [m["content"] for m in request["messages"][1:-1]] == [
            f"{label}-{i}" for i in range(10)
        ]


class RoutedLLM:
    def __init__(self, response):
        self.response, self.inputs = response, []

    def structured(self, system, payload, out):
        self.inputs.append(payload)
        return out.model_validate(self.response), {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "usd": 0.0,
        }


def test_lookup_with_model_calculation_runs_as_calculation():
    # 계산 여부는 LLM이 conditions로 정한다. 코드는 그 조건에 맞는 실행 경로만 고른다.
    llm = RoutedLLM(
        route_reply(
            kinds=["saving"],
            amount_won=500000,
            monthly=True,
            term_months=12,
            calculation="forward",
        )
    )
    result = asyncio.run(
        route_node(
            {"query": "월 50씩 12개월 적금 계산"},
            SimpleNamespace(llm=llm, catalog=CAT),
        )
    )
    assert result["route"] == "hard"
    assert result["slots"]["amount_won"] == 500000


def test_market_rank_uses_model_conditions_without_rule_inheritance():
    prev = extract("가나적금 월 50만원씩 12개월 이자 얼마야?", CAT).to_dict()
    llm = RoutedLLM(
        route_reply(
            "query_rates",
            scope="market",
            rank=2,
            follow_up=True,
            kinds=["saving"],
            term_months=12,
            metric="rate",
        )
    )
    result = asyncio.run(
        route_node(
            {"query": "두번째로 높은 상품", "prev_slots": prev},
            SimpleNamespace(llm=llm, catalog=CAT),
        )
    )
    assert result["slots"]["product_ids"] == []
    assert result["slots"]["term_months"] == 12
    assert result["slots"]["rank"] == 2
    assert result["slots"]["amount_won"] is None
    assert llm.inputs[0].previous_conditions["products"] == ["가은행 가나적금"]


def test_meaning_is_not_reinterpreted_by_keyword_rules(monkeypatch):
    from finagent.catalog import Catalog

    def forbidden(*args):
        raise AssertionError("규칙 해석을 호출했습니다")

    monkeypatch.setattr(Catalog, "find_banks", forbidden)
    monkeypatch.setattr(Catalog, "find_products", forbidden)
    monkeypatch.setattr("legacy_rules.extract", forbidden)
    query = (
        "상품을 하나도 몰라. 우대조건을 모두 채웠을 때 은행 예금 금리 1위는?"
    )
    llm = RoutedLLM(
        route_reply(
            "query_rates",
            scope="market",
            rank=1,
            kinds=["deposit"],
            group="bank",
            metric="rate_max",
            term_months=12,
        )
    )
    result = asyncio.run(
        route_node({"query": query}, SimpleNamespace(llm=llm, catalog=CAT))
    )
    assert result["slots"]["banks"] == []
    assert result["slots"]["metric"] == "rate_max"
    assert result["route"] == "hard"


def test_new_catalog_lookup_cannot_use_conversation_recall_path():
    llm = RoutedLLM(route_reply(kinds=["saving"], products=["가나적금"]))
    result = asyncio.run(
        route_node(
            {"query": "가나적금 우대조건 찾아줘"},
            SimpleNamespace(llm=llm, catalog=CAT),
        )
    )
    assert result["route"] == "easy"
    assert result["intent"] == "finance_new"


def test_model_invented_calculation_amount_is_rejected():
    import pytest

    llm = RoutedLLM(
        route_reply(
            "calculate",
            kinds=["saving"],
            amount_won=5000000,
            monthly=True,
            term_months=12,
            calculation="forward",
        )
    )
    with pytest.raises(LLMOutputError, match="계산 금액"):
        asyncio.run(
            route_node(
                {"query": "월 50만원씩 12개월 적금 계산"},
                SimpleNamespace(llm=llm, catalog=CAT),
            )
        )


def test_session_context_is_reset_after_error():
    import pytest

    with pytest.raises(RuntimeError):
        with session_context([{"role": "user", "content": "private"}]):
            raise RuntimeError("failure")
    assert not session_messages()


def test_rank_is_applied_before_limit_and_preserves_ties():
    assert CAT.query("saving", rank=1, limit=1)[0]["product"] == "다라적금"
    assert CAT.query("saving", rank=2, limit=1)[0]["product"] == "가나적금"
    assert CAT.query("saving", rank=3) == []


def test_followup_with_carried_amount_keeps_previous_calculation():
    prev = {"kinds": ["deposit"], "amount_won": 10_000_000, "calc": True}
    llm = RoutedLLM(
        route_reply(
            "query_rates",
            scope="market",
            rank=2,
            follow_up=True,
            kinds=["deposit"],
            amount_won=10_000_000,
            term_months=12,
            metric="rate",
        )
    )
    result = asyncio.run(
        route_node(
            {
                "query": "2위 상품이면 얼마나 차이나?",
                "prev_slots": prev,
                "history": [
                    {
                        "role": "user",
                        "content": "그거 1000만원 넣으면 세후 얼마야?",
                    }
                ],
            },
            SimpleNamespace(llm=llm, catalog=CAT),
        )
    )
    assert result["slots"]["calc"] and result["route"] == "hard"
