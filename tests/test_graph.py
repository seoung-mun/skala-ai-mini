"""v2 그래프 경로별 테스트. LLM·MCP·검색기를 가짜로 주입한다(실제 모델 없이 전체 흐름 검증)."""
import asyncio

from finagent import catalog as C
from finagent.catalog import Catalog
from finagent.deps import Deps
from finagent.graph.build import build_graph, run_v2
from finagent.parser import parse_rows
from finagent.schemas import ToolResult


def _saving(code, bank, name, rate, rate_max, cnd="급여 이체 시 연 0.5%p", term="12"):
    b = {"dcls_month": "202609", "fin_co_no": code, "fin_prdt_cd": code, "kor_co_nm": bank, "fin_prdt_nm": name,
         "join_way": "스마트폰", "dcls_end_day": "", "fin_co_subm_day": "", "mtrt_int": "", "spcl_cnd": cnd,
         "join_deny": "1", "join_member": "", "etc_note": "", "max_limit": None}
    o = {"dcls_month": "202609", "fin_co_no": code, "fin_prdt_cd": code, "intr_rate_type_nm": "단리",
         "rsrv_type_nm": "정액적립식", "save_trm": term, "intr_rate": rate, "intr_rate2": rate_max}
    return parse_rows("saving", "bank", [b], [o])[0]


CAT = Catalog([_saving("A1", "가은행", "가나적금", 2.45, 3.85), _saving("B1", "나은행", "다라적금", 3.2, 3.5)])
A, B = (p.id for p in CAT.products)


class FakeMCP:
    """MCP 서버와 같은 catalog 함수를 프로세스 안에서 호출하고 ToolResult로 돌려준다."""
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.fail:
            return ToolResult(ok=False, tool_name=name, error="boom"), 0
        if name == "query_products":
            return ToolResult(ok=True, tool_name=name, data={"rows": CAT.query(**args)}), 0
        if args.get("mode") == "goal":
            fn = {"saving": C.saving_goal, "deposit": C.deposit_goal}.get(args["product_type"])
            res = fn(args["amount_won"], args["term_months"], args["rate_pct"]) if fn else \
                C.loan_capacity(args["amount_won"], args["term_months"], args["rate_pct"], args["repayment"])
            return ToolResult(ok=True, tool_name=name, data={"name": "역산", "result": res, "after_tax": None,
                                                             "unit": "원", "inputs": args, "formula": "f"}), 0
        fn = {"saving": C.saving_interest, "deposit": C.deposit_interest}.get(args["product_type"])
        res = fn(args["amount_won"], args["term_months"], args["rate_pct"]) if fn else \
            C.loan_payment(args["amount_won"], args["term_months"], args["rate_pct"], args["repayment"])
        after = int(res * 0.846) if fn else None
        return ToolResult(ok=True, tool_name=name, data={"name": "이자", "result": res, "after_tax": after,
                                                         "unit": "원", "inputs": args, "formula": "f"}), 0

    async def get_prompt(self, name, args):
        return "절차"


class FakeRetriever:
    def __init__(self, ids):
        self.ids, self.calls = ids, []

    def search(self, q, k, allowed=None):
        self.calls.append(allowed)
        return [(i, 1.0) for i in self.ids if allowed is None or i in allowed][:k]


class FakeLLM:
    """structured 응답을 순서대로 돌려준다(dict -> 출력 모델로 검증). 받은 입력 모델도 기록한다."""
    def __init__(self, *replies):
        self.replies, self.inputs = list(replies), []

    def structured(self, system, payload, out):
        self.inputs.append(payload)
        return out.model_validate(self.replies.pop(0)), {"prompt_tokens": 10, "completion_tokens": 5, "usd": 0.001}

    def chat(self, system, user, schema=None):
        return self.replies.pop(0), {"prompt_tokens": 10, "completion_tokens": 5, "usd": 0.001}


def ans(reply, ok=True, cites=(), missing=""):
    return {"ok": ok, "reply": reply, "citations": list(cites), "missing": missing}


def run(q, llm, mcp=None, ids=(A,), prev=None):
    deps = Deps(llm=llm, mcp=mcp or FakeMCP(), retriever=FakeRetriever(list(ids)), catalog=CAT, data_month="202609")
    return asyncio.run(run_v2(build_graph(deps), deps, q, prev_slots=prev))


def types(state):
    return [e["type"] for e in state["events"]]


def test_chat_route_answers_without_retrieval():
    final, st = run("안녕! 반가워", FakeLLM({"ok": True, "reply": "안녕하세요!"}))
    assert final["route"] == "chat" and final["reply"] == "안녕하세요!" and types(st) == ["RouteDecided"]
    assert final["metadata"]["llm_calls"] == 1 and final["warnings"][0].startswith("2026년 9월")


def test_out_of_scope_refuses_without_llm():
    final, st = run("테슬라 주식 어때?", FakeLLM())
    assert final["route"] == "out_of_scope" and st["llm_calls"] == 0


def test_easy_path_uses_rag_subgraph_and_structured_input():
    llm = FakeLLM(ans("가나적금 급여 이체 우대는 연 0.5%p예요",
                      cites=[{"product_id": A, "section": "우대조건", "quote": "급여 이체 시 연 0.5%p"}]))
    final, st = run("가나적금 우대조건 뭐야?", llm)
    assert types(st) == ["RouteDecided", "TaskCompleted", "Validation"]
    assert final["citations"][0]["quote"] == "급여 이체 시 연 0.5%p" and final["products"][0]["id"] == A
    doc = llm.inputs[0].documents[0]  # LLM에는 표와 인용 가능한 설명이 분리돼 들어간다
    assert "|" in doc.rate_table and doc.descriptions[0].section == "우대조건" and llm.inputs[0].citable_ids == [A]


def test_bad_citation_is_dropped_with_warning_not_rejected():
    bad = ans("최고금리 3.85%예요", cites=[{"product_id": A, "section": "금리", "quote": "| 12 | 3.85 |"},
                                       {"product_id": A, "section": "우대조건", "quote": "급여 이체 시 연 0.5%p"}])
    final, st = run("가나적금 최고금리 얼마야?", FakeLLM(bad))
    assert st["verified"] and [c["quote"] for c in final["citations"]] == ["급여 이체 시 연 0.5%p"]
    assert any("인용 1건은 제외" in w for w in final["warnings"])


def test_wrong_number_is_rejected_then_fallback():
    bad = ans("최고금리 9.99%예요")
    final, st = run("가나적금 최고금리 얼마야?", FakeLLM(bad, bad))
    assert [e["verdict"] for e in st["events"] if e["type"] == "Validation"] == ["fail", "fail"]  # 재작성 1회 후 중단
    assert "검증된 답을 만들지 못했어요" in final["reply"]


def test_insufficient_evidence_stops_without_rewrite():
    final, st = run("가나적금 중도해지하면 어떻게 돼?", FakeLLM(ans("", ok=False, missing="중도해지 조건이 공시에 없어요")))
    assert st["llm_calls"] == 1 and "중도해지 조건이 공시에 없어요" in final["reply"]


def test_hard_calc_agent_resolves_rate_itself():
    mcp = FakeMCP()
    final, st = run("가나적금 정액적립식 12개월에 월 50만원씩 넣으면 기본금리로 세전이자 얼마야?",
                    FakeLLM(ans("기본금리 2.45%로 세전이자는 79,625원이에요")), mcp)
    assert final["route"] == "hard" and final["calculations"][0]["result"] == 79625
    assert [n for n, _ in mcp.calls] == ["query_products", "calculate"]  # 금리 조회 → 계산 (calc 서브그래프 안에서)
    assert final["products"][0]["id"] == A and final["metadata"]["validation_attempts"] == 1


def test_after_tax_answer_satisfies_required_calc():
    final, st = run("가나적금 정액적립식 12개월에 월 50만원씩 넣으면 세후 이자 얼마야?",
                    FakeLLM(ans(f"세후 {int(79625 * 0.846):,}원이에요")))
    assert st["verified"] is True


def test_compare_and_calc_run_as_parallel_subagents():
    mcp = FakeMCP()
    final, st = run("은행 적금 중에 12개월 기본금리 제일 높은 상품에 월 50만원 넣으면 이자 얼마야?",
                    FakeLLM(ans("다라적금이 3.2%로 가장 높고 이자는 104,000원이에요")), mcp)
    done = [e["agent"] for e in st["events"] if e["type"] == "TaskCompleted"]
    assert sorted(done) == ["calc", "structured"] and final["calculations"][0]["result"] == 104000
    assert final["products"][0]["product"] == "다라적금"


def test_hard_compare_replans_once_when_filter_returns_nothing():
    final, st = run("은행 적금 중에 24개월 기본금리 제일 높은 거 뭐야?", FakeLLM(ans("다라적금이 3.2%로 가장 높아요")))
    assert [e["is_sufficient"] for e in st["events"] if e["type"] == "ReplanCheck"] == [False, True]
    assert final["products"][0]["product"] == "다라적금"


def test_compare_answer_picking_non_top_product_is_rejected():
    final, st = run("은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?",
                    FakeLLM(ans("가나적금이 2.45%로 가장 높아요"), ans("다라적금이 3.2%로 가장 높아요")))
    reasons = [e["reason"] for e in st["events"] if e["type"] == "Validation"]
    assert reasons[0].startswith("조회 1위는 다라적금") and reasons[1] is None


def test_hard_fallback_table_when_validation_keeps_failing():
    bad = ans("다라적금이 9.9%예요")
    final, st = run("은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?", FakeLLM(bad, bad, bad))
    assert st["attempts"] == 3 and "3.2%" in final["reply"]
    assert any("검증을 통과하지 못해" in w for w in final["warnings"])


def test_credit_without_score_asks_instead_of_guessing():
    final, st = run("신용대출 금리 제일 낮은 곳 어디야?", FakeLLM())
    assert "신용점수" in final["reply"] and st["llm_calls"] == 0


def test_tool_failure_is_reported_not_hidden():
    final, st = run("은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?", FakeLLM(ans("모르겠어요", ok=False)),
                    FakeMCP(fail=True))
    assert any(e["type"] == "TaskCompleted" and e["ok"] is False for e in st["events"])
    assert any("boom" in w for w in final["warnings"])


def test_follow_up_inherits_previous_product():
    prev = {"kinds": ["saving"], "product_ids": [A], "banks": [], "group": None}
    final, st = run("그거 우대조건은?", FakeLLM(ans("연 0.5%p예요")), prev=prev)
    assert final["route"] == "easy" and st["slots"]["product_ids"] == [A]


def test_named_product_skips_retrieval_but_unnamed_question_searches():
    def run_with(q):
        r = FakeRetriever([A, B])
        deps = Deps(llm=FakeLLM(ans("연 0.5%p예요")), mcp=FakeMCP(), retriever=r, catalog=CAT, data_month="202609")
        _, st = asyncio.run(run_v2(build_graph(deps), deps, q))
        return r.calls, st
    calls, st = run_with("가나적금 우대조건 뭐야?")
    assert calls == [] and st["results"][0]["doc_ids"] == [A]  # 이름이 맞은 상품이 그대로 근거
    calls, st = run_with("적금 급여 이체 우대 얼마야?")
    assert calls == [{A, B}] and st["results"][0]["doc_ids"] == [A, B]


def test_graph_drawing_shows_subgraph_internals():
    deps = Deps(llm=FakeLLM(), mcp=FakeMCP(), retriever=FakeRetriever([A]), catalog=CAT, data_month="202609")
    mermaid = build_graph(deps).get_graph(xray=True).draw_mermaid()
    for node in ("structured_agent", "rag_agent", "calc_agent", "resolve_rate", "replan_check"):
        assert node in mermaid


def test_spread_question_gets_spread_row_and_rejects_max_rate_answer():
    """'우대 몇 %p'에 최고금리(3.85%)로 답하면 반려, 재작성도 실패하면 우대 폭 표로 대체(v2 오답 E2·S4·N10 유형)."""
    llm = FakeLLM(ans("가나적금 최고금리는 3.85%예요"), ans("우대 포함 3.85%예요"))
    final, st = run("가나적금 우대금리 최고 몇 %p야?", llm)
    assert [t["agent"] for t in st["tasks"]] == ["rag", "structured"]
    row = llm.inputs[0].rows[0]
    assert (row.value, row.unit) == (1.4, "%p") and "1.4%p" in llm.inputs[0].to_prompt()
    assert [e["verdict"] for e in st["events"] if e["type"] == "Validation"] == ["fail", "fail"]
    assert "우대 폭" in st["events"][-1]["reason"] or "우대 폭" in st["feedback"]
    assert "1.4%p" in final["reply"] and final["products"][0]["highlight"]["unit"] == "%p"


def test_spread_answer_passes():
    final, st = run("가나적금 우대금리 최고 몇 %p야?", FakeLLM(ans("가나적금 우대금리는 최고 1.4%p예요 (최고금리 3.85%)")))
    assert st["verified"] and final["reply"].startswith("가나적금 우대금리는 최고 1.4%p")


def test_goal_question_uses_reverse_calculation():
    need = C.saving_goal(10_000_000, 12, 3.0)
    llm = FakeLLM(ans(f"1년에 1,000만원을 모으려면 연 3.0%로 매달 {need:,}원씩 넣으면 돼요"))
    final, st = run("1년 동안 1000만원 모으려면 적금 연 3% 기준 매달 얼마 넣어야 해?", llm)
    calc = next(t for t in st["tasks"] if t["agent"] == "calc")
    assert st["route"] == "hard" and calc["args"]["mode"] == "goal" and calc["args"]["product_type"] == "saving"
    assert st["verified"] and final["calculations"][0]["result"] == need


def test_unnamed_spread_question_uses_top_retrieved_product():
    llm = FakeLLM(ans("다라적금 우대 폭은 최고 0.3%p예요"))
    final, st = run("나은행 환경 적금 우대 최대 몇 %p야?", llm, ids=(B, A))
    spread = [t for t in st["tasks"] if t["agent"] == "structured"]
    assert spread and spread[0]["args"]["product_ids"] == [B] and st["verified"]
