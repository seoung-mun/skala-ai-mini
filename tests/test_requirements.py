"""요청 완전성 회귀: 수치가 맞아도 상품별 가입대상·세전/세후 누락은 반려한다."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

from test_graph import CAT, A, B, FakeLLM, FakeMCP, FakeRetriever, ans

from finagent.deps import Deps
from finagent.graph.build import build_graph, run_v2
from finagent.graph.nodes import product_doc
from finagent.requirements import build_requirements, check_requirements
from finagent.schemas import AnswerInput, Calculation, RateRow


def sample():
    catalog = deepcopy(CAT)
    catalog.by_id[A].base["join_member"] = "실명의 개인(개인사업자 제외)"
    catalog.by_id[B].base["join_member"] = "만18세이상 여성 및 개인사업자"
    rows = [
        RateRow(
            product_id=p.id,
            bank=p.bank,
            product=p.name,
            option="12개월 단리",
            metric="기본금리",
            value=rate,
        )
        for p, rate in [(catalog.by_id[B], 3.2), (catalog.by_id[A], 2.45)]
    ]
    inp = AnswerInput(
        question="은행 적금 중 12개월 기본금리 높은 상품 2개와 각 가입대상을 알려줘. 1위에 월 50만원씩 12개월 넣으면 단리 세전·세후 이자도 알려줘.",
        rows=rows,
        documents=[
            product_doc(SimpleNamespace(catalog=catalog), r.product_id)
            for r in rows
        ],
        calculations=[
            Calculation(
                name="적금 이자",
                result=104000,
                after_tax=87984,
                inputs={
                    "product_type": "saving",
                    "mode": "forward",
                    "amount_won": 500000,
                    "term_months": 12,
                    "rate_pct": 3.2,
                },
                formula="공식",
            )
        ],
    )
    inp.required_facts = build_requirements(inp)
    return catalog, inp


COMPLETE = (
    "나은행 다라적금 기본금리 3.2%, 가입대상: 만18세이상 여성 및 개인사업자.\n"
    "가은행 가나적금 기본금리 2.45%, 가입대상: 실명의 개인(개인사업자 제외).\n"
    "세전 이자 104,000원, 세후 이자 87,984원입니다."
)


def test_checklist_rejects_missing_product_details_and_amounts():
    _, inp = sample()
    assert not check_requirements(inp, COMPLETE)
    missing = COMPLETE.replace("가입대상: 실명의 개인(개인사업자 제외).", "")
    assert any(
        "가나적금 가입 대상" in x for x in check_requirements(inp, missing)
    )
    for missing in [
        COMPLETE.replace("세후 이자 87,984원", ""),
        COMPLETE.replace("세전 이자 104,000원", ""),
        COMPLETE.replace("가은행 가나적금 기본금리 2.45%", "가은행 가나적금"),
        "당신은 방금 무엇을 질문했는지 물었습니다.",
    ]:
        assert check_requirements(inp, missing)
    assert "필수 완료 조건" in inp.to_prompt()


def test_details_cannot_be_swapped_between_products():
    _, inp = sample()
    swapped = (
        COMPLETE.replace("만18세이상 여성 및 개인사업자", "TEMP")
        .replace(
            "실명의 개인(개인사업자 제외)", "만18세이상 여성 및 개인사업자"
        )
        .replace("TEMP", "실명의 개인(개인사업자 제외)")
    )
    errors = check_requirements(inp, swapped)
    assert sum("필수 항목" in error for error in errors) == 2


def test_missing_source_requires_explicit_unavailable_without_fabricating():
    _, inp = sample()
    inp.documents[0].descriptions = [
        d for d in inp.documents[0].descriptions if d.section != "가입 대상"
    ]
    inp.required_facts = build_requirements(inp)
    # 다른 출처에 있는 조건을 공시되지 않은 상품에 붙여서는 안 된다.
    assert check_requirements(inp, COMPLETE)
    reply = COMPLETE.replace(
        "가입대상: 만18세이상 여성 및 개인사업자",
        "가입대상: 공시 정보가 없어 확인 불가",
    )
    assert not check_requirements(inp, reply)


def execute(*replies):
    catalog, inp = sample()
    llm = FakeLLM(*(ans(r) for r in replies))
    deps = Deps(
        llm=llm,
        mcp=FakeMCP(),
        retriever=FakeRetriever([A, B]),
        catalog=catalog,
        data_month="202609",
        planner="rule",
        rag_agent="fixed",
    )
    return asyncio.run(run_v2(build_graph(deps), deps, inp.question)), llm


def test_graph_rewrites_incomplete_answer_then_passes_complete_one():
    missing = COMPLETE.replace("가입대상: 실명의 개인(개인사업자 제외).", "")
    (final, state), llm = execute(missing, COMPLETE)
    assert [
        e["verdict"] for e in state["events"] if e["type"] == "Validation"
    ] == ["fail", "pass"]
    assert "가나적금 가입 대상" in llm.inputs[1].previous_rejection
    assert state["verified"] and final["reply"] == COMPLETE


def test_graph_exhaustion_uses_source_details_and_never_marks_incomplete_as_verified():
    bad = COMPLETE.replace("가입대상: 실명의 개인(개인사업자 제외).", "")
    (final, state), _ = execute(bad, bad, bad)
    assert not state["verified"]
    assert "실명의 개인(개인사업자 제외)" in final["reply"]
    assert "만18세이상 여성 및 개인사업자" in final["reply"]
    assert any("검증을 통과하지 못해" in w for w in final["warnings"])


def test_both_tax_labels_are_bound_to_their_own_amounts():
    _, inp = sample()
    swapped = (
        COMPLETE.replace("104,000원", "TEMP")
        .replace("87,984원", "104,000원")
        .replace("TEMP", "87,984원")
    )
    assert len(check_requirements(inp, swapped)) == 2


def test_requested_count_cannot_silently_shrink():
    _, inp = sample()
    inp.question = inp.question.replace("상품 2개", "상품 3개")
    inp.required_facts = build_requirements(inp)
    assert any("결과 개수" in e for e in check_requirements(inp, COMPLETE))
    assert not check_requirements(
        inp, COMPLETE + " 조건에 맞는 상품은 2개뿐입니다."
    )


def test_original_two_turns_with_real_catalog_and_mcp(tmp_path):
    """실데이터·실제 MCP, 모의 작성 모델: 가입대상 누락 반려 후 직전 질문 회상."""
    from pathlib import Path

    import pytest
    from fastapi.testclient import TestClient
    from test_server import parse_sse

    from finagent.catalog import Catalog, after_tax, deposit_interest
    from finagent.mcp_client import MCPClient
    from finagent.parser import parse_all
    from finagent.search import KeywordIndex
    from finagent.server import Runtime, create_app
    from finagent.session_store import SqliteSessionStore

    root = Path(__file__).resolve().parents[1]
    if not any((root / "data/raw").glob("*.json")):
        pytest.skip("data/raw 없음")
    catalog = Catalog(parse_all(root / "data/raw"))
    rows = catalog.query(
        "deposit", group="bank", term_months=24, metric="rate", limit=3
    )
    question = (
        "은행권 정기예금 중 24개월 기본금리가 높은 상품 3개와 각 가입대상을 알려줘. "
        "기본금리 1위 상품에 2,700만원을 넣었을 때 단리 세전·세후 이자도 계산해줘. 동률이면 숨기지 마."
    )
    amounts = deposit_interest(27000000, 24, rows[0]["value"], False)
    calculation = (
        f"세전 이자 {amounts:,}원, 세후 이자 {after_tax(amounts):,}원입니다."
    )
    lines = [
        f"{r['bank']} {r['product']} 기본금리 {r['value']}%, 가입대상: {catalog.by_id[r['product_id']].base['join_member']}"
        for r in rows
    ]
    bad = (
        "\n".join(
            [lines[0]]
            + [
                f"{r['bank']} {r['product']} 기본금리 {r['value']}%"
                for r in rows[1:]
            ]
        )
        + "\n"
        + calculation
    )
    complete = "\n".join(lines) + "\n" + calculation
    llm = FakeLLM(ans(bad), ans(complete))

    async def runtime():
        mcp = MCPClient()
        await mcp.start()
        deps = Deps(
            llm=llm,
            mcp=mcp,
            retriever=FakeRetriever([r["product_id"] for r in rows]),
            catalog=catalog,
            data_month="202609",
            planner="rule",
            rag_agent="fixed",
        )
        return Runtime(
            "v2",
            deps,
            KeywordIndex({rows[0]["product_id"]: rows[0]["product"]}),
            SqliteSessionStore(tmp_path / "isolated.db"),
            "test-writer",
            close=mcp.stop,
        )

    with TestClient(create_app(runtime)) as client:
        first = parse_sse(
            client.get(
                "/chat/stream",
                params={
                    "message": question,
                    "thread_id": "two-turn-regression",
                },
            ).text
        )
        assert [e["verdict"] for e in first if e["type"] == "Validation"] == [
            "fail",
            "pass",
        ]
        assert first[-1]["reply"] == complete
        previous_calls = len(llm.inputs)
        second = parse_sse(
            client.get(
                "/chat/stream",
                params={
                    "message": "내가 방금 뭐라고 질문했어?",
                    "thread_id": "two-turn-regression",
                },
            ).text
        )
        assert second[0]["intent"] == "recall" and second[-1]["route"] == "chat"
        assert question in second[-1]["reply"] and not second[-1]["products"]
        assert len(llm.inputs) == previous_calls
        assert not any(
            e["type"] in ("TaskCompleted", "Validation") for e in second
        )


def test_boundary_ties_keep_every_product_document_detail_and_card():
    """상위 3개 요청의 동률이 4개면 문서·가입대상·카드도 4개를 유지한다."""
    from test_graph import _saving

    from finagent.catalog import Catalog
    from finagent.schemas import ToolResult

    catalog = Catalog(
        [
            _saving(f"T{i}", f"{i}은행", f"동률적금{i}", 3.2, 3.5)
            for i in range(4)
        ]
    )
    for p in catalog.products:
        p.base["join_member"] = "실명의 개인"

    class LocalMCP(FakeMCP):
        async def call_tool(self, name, args):
            assert name == "query_products"
            return ToolResult(
                ok=True, tool_name=name, data={"rows": catalog.query(**args)}
            ), 0

    reply = "\n".join(
        f"{p.bank} {p.name} 기본금리 3.2%, 가입대상: 실명의 개인"
        for p in catalog.products
    )
    llm = FakeLLM(ans(reply))
    deps = Deps(
        llm=llm,
        mcp=LocalMCP(),
        retriever=FakeRetriever([p.id for p in catalog.products]),
        catalog=catalog,
        data_month="202609",
        planner="rule",
        rag_agent="fixed",
    )
    final, state = asyncio.run(
        run_v2(
            build_graph(deps),
            deps,
            "은행 적금 중 12개월 기본금리가 높은 상품 3개와 각 가입대상을 알려줘. 동률이면 모두 알려줘.",
        )
    )
    assert state["verified"] and len(final["products"]) == 4
    assert len(llm.inputs[0].documents) == 4
    assert (
        len(
            [
                f
                for f in llm.inputs[0].required_facts
                if f.section == "가입 대상"
            ]
        )
        == 4
    )


def test_compact_prompt_preserves_every_required_fact_and_keeps_other_explanations_full():
    _, inp = sample()
    prompt = inp.to_prompt()
    assert inp.compact_comparison
    assert "### 금리표" not in prompt
    for fact in inp.required_facts:
        assert fact.value in prompt
    assert "citations=[]" in prompt
    inp.question += " 우대조건과 중도해지 금리도 설명해줘."
    assert not inp.compact_comparison
    assert "### 금리표" in inp.to_prompt()


def test_sources_use_exact_document_and_section_without_inventing_missing_text():
    from finagent.requirements import source_citations
    from finagent.validate import split_citations

    _, inp = sample()
    doc = inp.documents[0]
    source = next(s for s in doc.descriptions if s.section == "가입 대상")
    citations = source_citations(
        inp,
        [
            {
                "product_id": doc.product_id,
                "section": "설명",
                "quote": f"- [가입 대상] {source.text}",
            }
        ],
    )
    assert len(citations) == 2
    assert citations[0] == {
        "product_id": doc.product_id,
        "section": "가입 대상",
        "quote": source.text,
    }
    sections = {
        d.product_id: "\n".join(s.text for s in d.descriptions)
        for d in inp.documents
    }
    named = {
        d.product_id: {s.section: s.text for s in d.descriptions}
        for d in inp.documents
    }
    kept, dropped = split_citations(citations, sections, named)
    assert len(kept) == 2 and not dropped
    wrong = {**citations[0], "section": "우대조건"}
    assert not split_citations([wrong], sections, named)[0]
    invented = {**citations[0], "quote": "누구나 가입 가능 (만든 내용)"}
    assert invented in source_citations(inp, [invented])
    assert split_citations([invented], sections, named)[1]
    for fact in inp.required_facts:
        if fact.kind == "text":
            fact.value = ""
    assert source_citations(inp, []) == []


def test_collected_deduplicates_calculation_rate_without_losing_distinct_options():
    from finagent.graph.nodes import collected

    row = {
        "product_id": "a",
        "metric": "rate",
        "option": "24개월 단리",
        "value": 3.94,
    }
    state = {
        "tasks": [
            {"id": "q", "agent": "structured"},
            {"id": "c", "agent": "calc"},
        ],
        "results": [
            {
                "id": "q",
                "ok": True,
                "rows": [row, {**row, "option": "12개월 단리"}],
            },
            {"id": "c", "ok": True, "calc": {}, "rate_row": row},
        ],
    }
    # Task results are keyed by task_id in graph state.
    for result in state["results"]:
        result["task_id"] = result.pop("id")
    rows, _, _, _ = collected(state)
    assert len(rows) == 2


def test_contradictory_membership_appendix_is_rejected_then_rewritten():
    bad = COMPLETE + "\n두 상품은 누구나 조건 없이 가입할 수 있습니다."
    (final, state), _ = execute(bad, COMPLETE)
    assert [
        e["verdict"] for e in state["events"] if e["type"] == "Validation"
    ] == ["fail", "pass"]
    assert state["verified"] and final["reply"] == COMPLETE


def test_unsupported_membership_claims_and_negated_source_cannot_pass():
    _, inp = sample()
    for claim in [
        "두 상품은 누구나 가입 가능합니다.",
        "남성도 가입할 수 있습니다.",
        "개인사업자도 가입 가능합니다.",
        "가입대상: 만18세이상 여성 및 개인사업자가 아닙니다.",
        "성별과 나이에 관계없이 이용할 수 있습니다.",
    ]:
        assert check_requirements(inp, COMPLETE + "\n" + claim), claim
    assert not check_requirements(inp, COMPLETE)


def test_membership_copy_from_another_product_and_source_negation_are_rejected():
    _, inp = sample()
    bad = (
        COMPLETE + "\n가은행 가나적금 가입대상: 만18세이상 여성 및 개인사업자."
    )
    assert check_requirements(inp, bad)
    negated = COMPLETE.replace(
        "가입대상: 실명의 개인(개인사업자 제외).",
        "가입대상은 실명의 개인(개인사업자 제외)가 아닙니다.",
    )
    assert check_requirements(inp, negated)
    (final, state), _ = execute(bad, bad, bad)
    assert not state["verified"]
    assert "누구나" not in final["reply"]
    assert final["reply"] != bad


def test_comparison_writer_does_not_require_unrequested_rag_candidates():
    from unittest.mock import patch

    from finagent.graph.nodes import write_node

    catalog, inp = sample()
    # 조회는 한 상품만 선택했지만 검색 후보에는 다른 상품도 포함된다.
    llm = FakeLLM(ans("답변"))
    deps = SimpleNamespace(catalog=catalog, llm=llm)
    row = inp.rows[0].model_dump() | {"metric": "rate"}
    state = {
        "query": inp.question,
        "slots": {"compare": True, "product_ids": []},
        "tasks": [
            {"id": "q", "agent": "structured"},
            {"id": "r", "agent": "rag"},
        ],
        "results": [
            {"task_id": "q", "ok": True, "rows": [row]},
            {
                "task_id": "r",
                "ok": True,
                "doc_ids": [r.product_id for r in inp.rows],
            },
        ],
    }
    with patch(
        "finagent.graph.nodes.get_stream_writer", return_value=lambda _: None
    ):
        output = asyncio.run(write_node(state, deps))
    docs = output["answer_input"]["documents"]
    assert [d["product_id"] for d in docs] == [row["product_id"]]
    assert all(
        f["product"] in ("", row["product"])
        for f in output["answer_input"]["required_facts"]
    )


def test_source_membership_in_bullets_is_accepted_and_prompt_keeps_units():
    _, inp = sample()
    reply = COMPLETE.replace("나은행", "- 나은행").replace("가은행", "- 가은행")
    reply = reply.replace("개인사업자.\n", "개인사업자\n")
    assert not check_requirements(inp, reply)
    assert "3.2%" in inp.to_prompt()
    assert "104000원" in inp.to_prompt()
    for row in inp.rows:
        row.metric = "우대 폭"
        row.unit = "%p"
    inp.required_facts = build_requirements(inp)
    assert "3.2%p" in inp.to_prompt()
