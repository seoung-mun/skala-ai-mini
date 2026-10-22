"""실제 공시의 오답 4종을 재현한다. LLM 응답만 고정하고 그래프·검증은 실제 실행한다."""

import asyncio
from pathlib import Path

import pytest
from legacy_rules import extract
from test_graph import FakeLLM, FakeRetriever, ans

from finagent.catalog import Catalog
from finagent.deps import Deps
from finagent.graph.build import build_graph, run_v2
from finagent.graph.nodes import route_node
from finagent.parser import parse_all
from finagent.schemas import ToolResult
from finagent.validate import check_answer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def catalog():
    """실제 공시 카탈로그(데이터가 없으면 skip)."""
    if not any((ROOT / "data/raw").glob("*.json")):
        pytest.skip("공시 데이터 없음")
    return Catalog(parse_all(ROOT / "data/raw"))


class QueryMCP:
    """query_products만 실제 카탈로그로 처리하는 가짜 MCP."""

    def __init__(self, catalog):
        """조회에 쓸 카탈로그를 받는다."""
        self.catalog = catalog

    async def call_tool(self, name, args):
        """query_products를 카탈로그로 실행해 ToolResult로 돌려준다."""
        assert name == "query_products"
        return ToolResult(
            ok=True, tool_name=name, data={"rows": self.catalog.query(**args)}
        ), 0


def run(catalog, query, *replies, prev=None):
    """LLM 답만 고정하고 실제 그래프로 질문을 실행한다. (final, state, llm)을 돌려준다."""
    llm = FakeLLM(
        *(
            [{"ok": True, "route": "easy", "reason": "후속 금리 조회"}]
            if prev
            else []
        ),
        *map(ans, replies),
    )
    deps = Deps(
        llm,
        QueryMCP(catalog),
        FakeRetriever([]),
        catalog,
        "202609",
        "rule",
        "fixed",
    )
    final, state = asyncio.run(
        run_v2(build_graph(deps), deps, query, prev_slots=prev)
    )
    return final, state, llm


@pytest.mark.parametrize(
    "kind,code", [("일반신용대출", "1"), ("마이너스한도대출", "2")]
)
def test_same_name_credit_documents_are_filtered_by_loan_type(
    catalog, kind, code
):
    """이름이 같은 신용대출은 질문의 대출 종류(일반·마이너스)로 걸러진다."""
    s = extract(
        f"우리은행 협약금리 外 신용대출상품 {kind} 신용점수 950점 금리", catalog
    )
    assert s.product_ids
    assert {catalog.by_id[i].base["crdt_prdt_type"] for i in s.product_ids} == {
        code
    }


def test_credit_answer_uses_selected_grade_and_type_not_another_rate(catalog):
    """신용대출 답의 근거는 질문한 점수 구간·대출 종류의 금리 하나뿐이다."""
    final, state, llm = run(
        catalog,
        "우리은행 협약금리 外 신용대출상품 일반신용대출 신용점수 950점 금리",
        "금리는 5.86%입니다.",
    )
    assert state["verified"] and final["reply"] == "금리는 5.86%입니다."
    assert {r.value for r in llm.inputs[0].rows} == {5.86}
    assert len(llm.inputs[0].documents) == 1


@pytest.mark.parametrize(
    "query,wrong,correct,reason",
    [
        (
            "월급 받는 통장을 우리은행으로 옮기면 우리SUPER주거래적금 "
            "금리 얼마나 더 붙어?",
            "급여이체 우대는 0.3%p입니다.",
            "급여이체 우대는 0.7%p입니다.",
            "급여",
        ),
        (
            "부산은행 더(The) 특판 정기예금 신규고객 우대이율 얼마야?",
            "신규고객 우대는 최대 1.95%p입니다.",
            "신규고객 우대는 1.25%p입니다.",
            "신규",
        ),
    ],
)
def test_specific_condition_rejects_overall_rate_and_rewrites(
    catalog, query, wrong, correct, reason
):
    """개별 우대조건 질문에 다른 조건·전체 우대 폭의 값으로 답하면 반려되고 재작성에서 고쳐진다."""
    final, state, llm = run(catalog, query, wrong, correct)
    validations = [e for e in state["events"] if e["type"] == "Validation"]
    assert [e["verdict"] for e in validations] == ["fail", "pass"]
    assert reason in validations[0]["reason"]
    assert final["reply"] == correct and state["verified"]
    assert reason in llm.inputs[0].to_prompt()


@pytest.mark.parametrize(
    "reply",
    [
        "급여이체 우대는 0.7%p입니다. 기본금리 2.45%에 우대 0.7%p가 "
        "더해져 최고금리는 3.85%입니다.",
        "2.45% + 0.7%p = 3.85%입니다.",
        "최고금리 3.85%에서 우대 0.7%p를 빼면 기본금리 2.45%입니다.",
        "2.45 + 0.7 = 3.85%입니다.",
    ],
)
def test_unsupported_rate_arithmetic_is_not_verified_even_if_each_number_is_evidence(
    reply,
):
    """숫자가 각각 근거에 있어도 금리 합산·차감 설명은 반려된다."""
    ok, why = check_answer(
        reply, {"pct": {2.45, 0.7, 3.85}, "won": set()}, [], {}
    )
    assert not ok and "합산" in why


def test_quoting_rates_without_arithmetic_is_allowed():
    """계산 없이 금리를 나열만 하면 통과한다."""
    assert check_answer(
        "급여 우대 0.7%p입니다. 기본금리 2.45%, 최고금리 3.85%입니다.",
        {"pct": {2.45, 0.7, 3.85}, "won": set()},
        [],
        {},
    ) == (True, None)


def test_arithmetic_failure_rewrites_and_never_reaches_final_reply(catalog):
    """합산 설명은 재작성되어 최종 답에 남지 않는다."""
    bad = "급여 우대 0.7%p입니다. 기본금리 2.45%에 0.7%p가 더해져 3.85%입니다."
    final, state, _ = run(
        catalog,
        "우리SUPER주거래적금 급여이체 우대 얼마야?",
        bad,
        "급여 우대는 0.7%p입니다.",
    )
    assert state["verified"] and "더해" not in final["reply"]
    assert any(
        e.get("verdict") == "fail" and "합산" in e["reason"]
        for e in state["events"]
    )


def test_followup_keeps_metric_filters_and_updates_term(catalog):
    """후속 질문은 직전 금리 종류·필터를 이어받고 기간만 바꾼다."""
    prev = extract(
        "우리SUPER주거래적금 정액적립식 12개월 기본금리 얼마야?", catalog
    ).to_dict()
    deps = Deps(
        FakeLLM({"ok": True, "route": "easy", "reason": "후속 금리 조회"}),
        QueryMCP(catalog),
        FakeRetriever([]),
        catalog,
        "202609",
        "rule",
        "fixed",
    )
    state = asyncio.run(
        route_node({"query": "그거 24개월은?", "prev_slots": prev}, deps)
    )
    assert (
        state["slots"]["metric"] == "rate"
        and state["slots"]["term_months"] == 24
    )
    assert state["slots"]["filters"] == {"rsrv_type_nm": "정액적립식"}
    changed = asyncio.run(
        route_node(
            {"query": "그거 36개월 최고금리는?", "prev_slots": prev}, deps
        )
    )
    assert (
        changed["slots"]["metric"] == "rate_max"
        and changed["slots"]["term_months"] == 36
    )


def test_followup_rejects_previous_term_rate_and_uses_current_term(catalog):
    """후속 질문 답에 직전 기간의 금리를 쓰면 반려되고 이번 기간 금리로 고쳐진다."""
    prev = extract(
        "우리SUPER주거래적금 12개월 기본금리 얼마야?", catalog
    ).to_dict()
    final, state, llm = run(
        catalog,
        "그거 24개월은?",
        "기본금리는 2.45%입니다.",
        "24개월 기본금리는 2.4%입니다.",
        prev=prev,
    )
    assert {r.value for r in llm.inputs[0].rows} == {2.4}
    assert (
        state["verified"] and final["reply"] == "24개월 기본금리는 2.4%입니다."
    )
    assert [
        e["verdict"] for e in state["events"] if e["type"] == "Validation"
    ] == ["fail", "pass"]


def test_condition_matching_does_not_override_explicit_rate_or_guess_ambiguous_clause(
    catalog,
):
    """기본·최고금리를 명시했거나 조건이 모호하면 조건 금리를 확정하지 않는다."""
    from finagent.graph.nodes import product_doc
    from finagent.schemas import Description
    from finagent.validate import condition_rate

    pid = extract("우리SUPER주거래적금", catalog).product_ids[0]
    deps = Deps(None, None, None, catalog, "202609", "rule", "fixed")
    doc = product_doc(deps, pid)
    assert condition_rate("급여이체하면 기본금리는?", [doc]) is None
    assert condition_rate("급여이체하면 최고금리는?", [doc]) is None
    # 두 조건이 똑같이 겹치면 어느 쪽인지 정하지 않는다.
    assert (
        condition_rate("급여이체랑 공과금 자동이체 우대 얼마?", [doc]) is None
    )
    doc.descriptions = [
        Description(section="우대조건", text="급여 이체 0.5%p 또는 0.7%p")
    ]
    assert condition_rate("급여이체 우대 얼마?", [doc]) is None


def test_unsupported_arithmetic_never_passes_after_retry_limit(catalog):
    """재작성 한도까지 합산식을 쓰면 끝내 통과하지 못하고 최종 답에서 빠진다."""
    bad = "급여 우대 0.7%p입니다. 2.45% + 0.7%p = 3.85%입니다."
    final, state, _ = run(
        catalog, "우리SUPER주거래적금 급여이체 우대 얼마야?", bad, bad
    )
    assert state["verified"] is False and "=" not in final["reply"]
    assert [
        e["verdict"] for e in state["events"] if e["type"] == "Validation"
    ] == ["fail", "fail"]


def test_maximum_preferential_margin_is_not_replaced_with_total_rate(catalog):
    """최대 우대금리(%p) 질문의 답이 최고금리로 바뀌지 않고 통과한다."""
    final, state, _ = run(
        catalog,
        "수협은행 Sh첫만남우대예금 최대 우대금리 얼마야?",
        "최대 우대금리는 1.05%p입니다.",
    )
    assert (
        state["verified"] and final["reply"] == "최대 우대금리는 1.05%p입니다."
    )
    assert not any(t["agent"] == "structured" for t in state["tasks"])


def test_named_loan_min_rate_has_structured_evidence_and_safe_fallback(catalog):
    """이름 있는 대출의 최저금리는 조회 값이 근거이고, 틀린 답은 도구 결과로 대체된다."""
    final, state, llm = run(
        catalog,
        "국민은행 KB주택전세자금대출 최저금리 얼마야?",
        "4.55%입니다.",
        "4.55%입니다.",
    )
    assert {r.value for r in llm.inputs[0].rows} == {4.21}
    assert "4.21%" in final["reply"] and "4.55%" not in final["reply"]
