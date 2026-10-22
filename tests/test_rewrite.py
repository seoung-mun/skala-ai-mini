"""후속 질문 재작성: 직전 대화가 있을 때만 LLM으로 독립 질문을 만들고, 직전 답의 숫자는 들이지 않는다."""

import asyncio

from test_graph import CAT, FakeLLM, FakeMCP, FakeRetriever, ans, route_reply

from finagent.deps import Deps
from finagent.graph.build import build_graph, run_v2

A, B = (p.id for p in CAT.products)
PREV = {
    "kinds": ["saving"],
    "product_ids": [A, B],
    "banks": [],
    "group": None,
    "term_months": 12,
    "metric": "rate",
}
HIST = [
    {
        "role": "user",
        "content": "가나적금이랑 다라적금 중에 12개월 기본금리 더 높은 거 뭐야?",
    },
    {"role": "assistant", "content": "나은행 다라적금이 3.2%로 더 높아요."},
]


def run(q, llm, history=HIST, prev=PREV):
    """대화 이력과 직전 슬롯을 넣어 그래프를 실행한다."""
    deps = Deps(
        llm=llm,
        mcp=FakeMCP(),
        retriever=FakeRetriever([A, B]),
        catalog=CAT,
        data_month="202609",
        planner="rule",
        rag_agent="fixed",
    )
    return asyncio.run(
        run_v2(build_graph(deps), deps, q, history=history, prev_slots=prev)
    )


def rw(question, follow_up=True):
    """RewriteOutput 형식의 고정 응답 dict."""
    return {
        "follow_up": follow_up,
        "question": question,
        "reason": "직전 1위 상품을 이어받음",
    }


def test_followup_model_selects_winner_without_rewriting_user_question():
    q = "그거 최고금리는?"
    llm = FakeLLM(
        route_reply(
            "query_rates",
            follow_up=True,
            kinds=["saving"],
            products=["다라적금"],
            term_months=12,
            metric="rate_max",
        ),
        ans("나은행 다라적금 12개월 최고금리는 3.5%예요"),
    )
    final, state = run(q, llm)
    assert state["query"] == q and not state.get("original_query")
    assert "rewrite" not in state["events"][0]
    assert state["slots"]["product_ids"] == [B]
    assert state["slots"]["metric"] == "rate_max"
    assert llm.inputs[0].question == q
    assert final["reply"].endswith("3.5%예요")


def test_unknown_reference_asks_the_model_question_without_lookup():
    q = "그거 우대조건은?"
    llm = FakeLLM(
        route_reply(
            follow_up=False,
            clarify="어떤 저축을 생각하고 계세요? 목돈인지 매달 저축인지 알려주세요.",
        )
    )
    final, state = run(q, llm, history=[], prev=None)
    assert final["reply"].startswith("어떤 저축")
    assert not final["products"] and not state["tasks"]
    assert state["llm_calls"] == 1


def test_standalone_question_keeps_original_and_only_one_routing_call():
    q = "가나적금 우대조건 알려줘"
    llm = FakeLLM(ans("가은행 가나적금 급여 이체 우대는 연 0.5%p예요"))
    _, state = run(q, llm)
    assert state["query"] == q
    assert "rewrite" not in state["events"][0]
    assert state["llm_calls"] == 2


def test_recall_returns_previous_user_question_without_llm_or_products():
    final, st = run(
        "내가 방금 뭐라고 질문했어?",
        FakeLLM({"ok": True, "reply": HIST[0]["content"]}),
    )
    assert HIST[0]["content"] in final["reply"]
    assert "내가 방금 뭐라고 질문했어?" not in final["reply"]
    assert final["route"] == "chat" and not final["products"]
    assert st["llm_calls"] == 2 and not st.get("tasks")
    assert not st["slots"]["kinds"]


def test_recall_with_no_previous_turn_does_not_invent_memory():
    final, st = run(
        "내 이전 질문 다시 알려줘", FakeLLM(), history=[], prev=None
    )
    assert "이전 질문" in final["reply"] and "없" in final["reply"]
    assert not final["products"] and st["llm_calls"] == 1


def test_recall_first_product_uses_first_user_turn_without_financial_lookup():
    history = HIST + [{"role": "user", "content": "그 상품 24개월은?"}]
    final, st = run(
        "내가 처음 물어본 상품 이름이 뭐였지?",
        FakeLLM({"ok": True, "reply": HIST[0]["content"]}),
        history=history,
    )
    assert HIST[0]["content"] in final["reply"]
    assert history[-1]["content"] not in final["reply"]
    assert final["route"] == "chat" and not final["products"]
    assert st["llm_calls"] == 2


def test_recall_credit_score_after_reset_does_not_search_products():
    final, st = run(
        "내가 아까 알려준 신용점수가 몇 점이었지?",
        FakeLLM(),
        history=[],
        prev=None,
    )
    assert "기록이 없어요" in final["reply"]
    assert final["route"] == "chat" and not final["products"]
    assert st["llm_calls"] == 1


def test_thanks_after_finance_does_not_inherit_product_slots():
    final, st = run(
        "고마워", FakeLLM({"ok": True, "reply": "도움이 되어 다행이에요."})
    )
    assert final["route"] == "chat" and not final["products"]
    assert not st["slots"]["kinds"] and st["llm_calls"] == 2


def test_negative_followup_decision_does_not_inherit_slots():
    llm = FakeLLM(
        {"ok": True, "route": "chat", "reason": "용어 설명"},
        {"ok": True, "reply": "금리는 이자의 비율이에요."},
    )
    final, st = run("금리가 뭐야?", llm)
    assert final["route"] == "chat" and not st["slots"]["kinds"]
    assert st["slots"]["term_months"] is None


def test_followup_bank_change_does_not_reuse_old_product_ids():
    q = "나은행은?"
    _, state = run(
        q, FakeLLM(rw(q), ans("나은행 다라적금 기본금리는 3.2%입니다."))
    )
    assert state["slots"]["banks"] == ["나은행"]
    assert state["slots"]["product_ids"] == []
    assert state["slots"]["term_months"] == 12
    assert state["events"][0]["intent"] == "finance_followup"
