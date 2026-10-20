"""v2 그래프 경로별 테스트. LLM·MCP·검색기를 가짜로 주입한다(실제 모델 없이 전체 흐름 검증)."""
import asyncio
import json

from finagent import catalog as C
from finagent.catalog import Catalog
from finagent.deps import Deps
from finagent.graph.build import build_graph, run_v2
from finagent.mcp_client import ToolFailed
from finagent.parser import parse_rows


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
    """MCP 서버와 같은 catalog 함수를 프로세스 안에서 호출한다."""
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.fail:
            raise ToolFailed(f"{name}: boom")
        if name == "query_products":
            return {"rows": CAT.query(**args)}, 0
        fn = {"calc_saving_interest": C.saving_interest, "calc_loan_payment": C.loan_payment,
              "calc_deposit_interest": C.deposit_interest}[name]
        return {"name": name, "result": fn(**args), "unit": "원", "inputs": args, "formula": "f"}, 0

    async def get_prompt(self, name, args):
        return "절차"


class FakeRetriever:
    def __init__(self, ids):
        self.ids = ids

    def search(self, q, k, allowed=None):
        return [(i, 1.0) for i in self.ids if allowed is None or i in allowed][:k]


class FakeLLM:
    """응답을 순서대로 돌려준다. dict는 JSON으로 직렬화한다."""
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def chat(self, system, user, schema=None):
        self.prompts.append(user)
        r = self.replies.pop(0)
        return (json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r), \
            {"prompt_tokens": 10, "completion_tokens": 5, "usd": 0.001}


def run(q, llm, mcp=None, ids=(A,), prev=None):
    deps = Deps(llm=llm, mcp=mcp or FakeMCP(), retriever=FakeRetriever(list(ids)), catalog=CAT, data_month="202609")
    return asyncio.run(run_v2(build_graph(deps), deps, q, prev_slots=prev))


def types(state):
    return [e["type"] for e in state["events"]]


def test_chat_route_answers_without_retrieval():
    final, st = run("안녕! 반가워", FakeLLM("안녕하세요!"))
    assert final["route"] == "chat" and final["reply"] == "안녕하세요!" and types(st) == ["RouteDecided"]
    assert final["metadata"]["llm_calls"] == 1 and final["warnings"][0].startswith("2026년 9월")


def test_out_of_scope_refuses_without_llm():
    final, st = run("테슬라 주식 어때?", FakeLLM())
    assert final["route"] == "out_of_scope" and st["llm_calls"] == 0


def test_easy_path_validates_number_and_quote():
    ok = {"reply": "가나적금 급여 이체 우대는 연 0.5%p예요", "citations": [
        {"product_id": A, "section": "우대조건", "quote": "급여 이체 시 연 0.5%p"}]}
    final, st = run("가나적금 우대조건 뭐야?", FakeLLM(ok))
    assert final["route"] == "easy" and types(st) == ["RouteDecided", "TaskCompleted", "Validation"]
    assert final["citations"][0]["quote"] == "급여 이체 시 연 0.5%p" and final["products"][0]["id"] == A


def test_easy_rewrite_then_fallback_after_limit():
    bad = {"reply": "우대는 연 9.9%p예요", "citations": []}
    final, st = run("가나적금 우대조건 뭐야?", FakeLLM(bad, bad))
    assert [e["verdict"] for e in st["events"] if e["type"] == "Validation"] == ["fail", "fail"]  # 재작성 1회 후 중단
    assert "검증된 답을 만들지 못했어요" in final["reply"] and final["citations"] == []


def test_hard_calc_uses_structured_rate_and_requires_result_in_answer():
    good = {"reply": "가나적금 기본금리 2.45%로 세전이자는 79,625원이에요", "citations": []}
    mcp = FakeMCP()
    final, st = run("가나적금 정액적립식 12개월에 월 50만원씩 넣으면 기본금리로 세전이자 얼마야?", FakeLLM(good), mcp)
    assert final["route"] == "hard" and final["calculations"][0]["result"] == 79625
    assert [n for n, _ in mcp.calls] == ["query_products", "calc_saving_interest"]
    assert final["metadata"]["validation_attempts"] == 1


def test_hard_compare_replans_once_when_filter_returns_nothing():
    good = {"reply": "다라적금이 3.2%로 가장 높아요", "citations": []}
    final, st = run("은행 적금 중에 24개월 기본금리 제일 높은 거 뭐야?", FakeLLM(good))
    checks = [e["is_sufficient"] for e in st["events"] if e["type"] == "ReplanCheck"]
    assert checks == [False, True] and final["products"][0]["product"] == "다라적금"


def test_hard_fallback_table_when_validation_keeps_failing():
    bad = {"reply": "다라적금이 9.9%예요", "citations": []}
    final, st = run("은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?", FakeLLM(bad, bad, bad))
    assert st["attempts"] == 3 and "3.2%" in final["reply"]  # 최초 1회 + 재작성 2회 후 도구 결과 표
    assert any("검증을 통과하지 못해" in w for w in final["warnings"])


def test_credit_without_score_asks_instead_of_guessing():
    final, st = run("신용대출 금리 제일 낮은 곳 어디야?", FakeLLM())
    assert "신용점수" in final["reply"] and st["llm_calls"] == 0


def test_tool_failure_is_reported_not_hidden():
    final, st = run("은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?", FakeLLM({"reply": "x", "citations": []}),
                    FakeMCP(fail=True))
    assert any(e["type"] == "TaskCompleted" and e["ok"] is False for e in st["events"])
    assert any("boom" in w for w in final["warnings"])


def test_bad_json_from_llm_is_a_validation_failure():
    final, st = run("가나적금 우대조건 뭐야?", FakeLLM("not json", "still not json"))
    reasons = [e["reason"] for e in st["events"] if e["type"] == "Validation"]
    assert all("JSON" in r for r in reasons)


def test_follow_up_inherits_previous_product():
    ok = {"reply": "연 0.5%p예요", "citations": []}
    prev = {"kinds": ["saving"], "product_ids": [A], "banks": [], "group": None}
    final, st = run("그거 우대조건은?", FakeLLM(ok), prev=prev)
    assert final["route"] == "easy" and st["slots"]["product_ids"] == [A]


def test_compare_answer_picking_non_top_product_is_rejected():
    wrong = {"reply": "가나적금이 2.45%로 가장 높아요", "citations": []}
    right = {"reply": "다라적금이 3.2%로 가장 높아요", "citations": []}
    final, st = run("은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?", FakeLLM(wrong, right))
    reasons = [e["reason"] for e in st["events"] if e["type"] == "Validation"]
    assert reasons[0].startswith("조회 1위는 다라적금") and reasons[1] is None
