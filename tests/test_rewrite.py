"""후속 질문 재작성: 직전 대화가 있을 때만 LLM으로 독립 질문을 만들고, 직전 답의 숫자는 들이지 않는다."""

import asyncio

from test_graph import CAT, FakeLLM, FakeMCP, FakeRetriever, ans

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


def test_follow_up_is_rewritten_to_the_previous_winner():
    """'그거' 후속 질문은 직전 1위 상품을 넣은 독립 질문으로 재작성된다."""
    llm = FakeLLM(
        rw("다라적금 12개월 최고금리 얼마야?"),
        ans("나은행 다라적금 12개월 최고금리는 3.5%예요"),
    )
    _, st = run("그거 최고금리는?", llm)
    decided = st["events"][0]
    assert (
        decided["type"] == "RouteDecided"
        and decided["rewrite"]["applied"] is True
    )
    assert (
        st["query"] == "다라적금 12개월 최고금리 얼마야?"
        and st["original_query"] == "그거 최고금리는?"
    )
    assert (
        st["slots"]["product_ids"] == [B]
        and st["slots"]["metric"] == "rate_max"
    )  # 두 상품이 아니라 1위 하나
    assert (
        llm.inputs[1].question == "다라적금 12개월 최고금리 얼마야?"
    )  # 작성 LLM도 재작성된 질문을 본다


def test_rewrite_with_numbers_from_the_previous_answer_is_rejected():
    """직전 답의 숫자가 들어간 재작성은 버리고 원문을 쓴다."""
    llm = FakeLLM(
        rw("금리 3.2% 상품 24개월은?"),
        {"ok": False, "reply": "", "citations": [], "missing": "24개월 금리 없음"},
    )
    _, st = run("그거 24개월은?", llm)
    rec = st["events"][0]["rewrite"]
    assert (
        rec["applied"] is False
        and "3.2" in rec["reason"]
        and st["query"] == "그거 24개월은?"
    )
    assert st["slots"]["product_ids"] == [
        A,
        B,
    ]  # 재작성을 버리면 기존 슬롯 이어받기로 처리


def test_no_history_or_standalone_question_skips_rewrite():
    """독립 질문은 재작성 LLM을 부르지 않는다."""
    llm = FakeLLM(ans("가은행 가나적금 급여 이체 우대는 연 0.5%p예요"))
    _, st = run(
        "가나적금 우대조건 알려줘", llm
    )  # 상품명이 있고 지시어가 없으면 독립 질문
    assert "rewrite" not in st["events"][0] and len(llm.inputs) == 1
    llm = FakeLLM(ans("가은행 가나적금 급여 이체 우대는 연 0.5%p예요"))
    _, st = run("그거 우대조건은?", llm, history=[])
    assert "rewrite" not in st["events"][0]


def test_recall_returns_previous_user_question_without_llm_or_products():
    final, st = run("내가 방금 뭐라고 질문했어?", FakeLLM())
    assert HIST[0]["content"] in final["reply"]
    assert "내가 방금 뭐라고 질문했어?" not in final["reply"]
    assert final["route"] == "chat" and not final["products"]
    assert st["llm_calls"] == 0 and not st.get("tasks")
    assert not st["slots"]["kinds"]


def test_recall_with_no_previous_turn_does_not_invent_memory():
    final, st = run(
        "내 이전 질문 다시 알려줘", FakeLLM(), history=[], prev=None
    )
    assert "이전 질문" in final["reply"] and "없" in final["reply"]
    assert not final["products"] and st["llm_calls"] == 0


def test_thanks_after_finance_does_not_inherit_product_slots():
    final, st = run(
        "고마워", FakeLLM({"ok": True, "reply": "도움이 되어 다행이에요."})
    )
    assert final["route"] == "chat" and not final["products"]
    assert not st["slots"]["kinds"] and st["llm_calls"] == 1


def test_negative_followup_decision_does_not_inherit_slots():
    llm = FakeLLM(
        rw("금리가 뭐야?", follow_up=False),
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
