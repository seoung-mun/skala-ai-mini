"""v2 그래프 경로별 테스트. LLM·MCP·검색기를 가짜로 주입한다(실제 모델 없이 전체 흐름 검증)."""

import asyncio

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from finagent import catalog as C
from finagent.catalog import Catalog
from finagent.deps import Deps
from finagent.evidence import EvidenceError, EvidenceTools
from finagent.graph.build import build_graph, run_v2
from finagent.parser import parse_rows
from finagent.schemas import ToolResult


def _saving(
    code, bank, name, rate, rate_max, cnd="급여 이체 시 연 0.5%p", term="12"
):
    """테스트용 은행 적금 Product를 원본 행 형식으로 만든다."""
    b = {
        "dcls_month": "202609",
        "fin_co_no": code,
        "fin_prdt_cd": code,
        "kor_co_nm": bank,
        "fin_prdt_nm": name,
        "join_way": "스마트폰",
        "dcls_end_day": "",
        "fin_co_subm_day": "",
        "mtrt_int": "",
        "spcl_cnd": cnd,
        "join_deny": "1",
        "join_member": "",
        "etc_note": "",
        "max_limit": None,
    }
    o = {
        "dcls_month": "202609",
        "fin_co_no": code,
        "fin_prdt_cd": code,
        "intr_rate_type_nm": "단리",
        "rsrv_type_nm": "정액적립식",
        "save_trm": term,
        "intr_rate": rate,
        "intr_rate2": rate_max,
    }
    return parse_rows("saving", "bank", [b], [o])[0]


CAT = Catalog(
    [
        _saving("A1", "가은행", "가나적금", 2.45, 3.85),
        _saving("B1", "나은행", "다라적금", 3.2, 3.5),
    ]
)
A, B = (p.id for p in CAT.products)


class FakeMCP:
    """MCP 서버와 같은 catalog 함수를 프로세스 안에서 호출하고 ToolResult로 돌려준다."""

    def __init__(self, fail=False):
        """호출 기록을 비운다. fail=True면 모든 도구 호출이 실패한다."""
        self.calls, self.fail = [], fail

    async def call_tool(self, name, args):
        """도구 호출을 기록하고 테스트 카탈로그로 결과를 만든다."""
        self.calls.append((name, args))
        if self.fail:
            return ToolResult(ok=False, tool_name=name, error="boom"), 0
        if name == "query_products":
            return ToolResult(
                ok=True, tool_name=name, data={"rows": CAT.query(**args)}
            ), 0
        if args.get("mode") == "goal":
            fn = {"saving": C.saving_goal, "deposit": C.deposit_goal}.get(
                args["product_type"]
            )
            res = (
                fn(args["amount_won"], args["term_months"], args["rate_pct"])
                if fn
                else C.loan_capacity(
                    args["amount_won"],
                    args["term_months"],
                    args["rate_pct"],
                    args.get("repayment", "equal_payment"),
                )
            )
            return ToolResult(
                ok=True,
                tool_name=name,
                data={
                    "name": "역산",
                    "result": res,
                    "after_tax": None,
                    "unit": "원",
                    "inputs": args,
                    "formula": "f",
                },
            ), 0
        fn = {"saving": C.saving_interest, "deposit": C.deposit_interest}.get(
            args["product_type"]
        )
        res = (
            fn(args["amount_won"], args["term_months"], args["rate_pct"])
            if fn
            else C.loan_payment(
                args["amount_won"],
                args["term_months"],
                args["rate_pct"],
                args.get("repayment", "equal_payment"),
            )
        )
        after = int(res * 0.846) if fn else None
        return ToolResult(
            ok=True,
            tool_name=name,
            data={
                "name": "이자",
                "result": res,
                "after_tax": after,
                "unit": "원",
                "inputs": args,
                "formula": "f",
            },
        ), 0

    async def get_prompt(self, name, args):
        """고정 절차 문구를 돌려준다."""
        return "절차"


class FakeRetriever:
    """정해진 id를 순서대로 돌려주는 가짜 검색기(allowed 인자를 기록)."""

    def __init__(self, ids):
        """돌려줄 id 목록을 받는다."""
        self.ids, self.calls = ids, []

    def search(self, q, k, allowed=None):
        """allowed를 기록하고 허용 범위 안의 id를 k개까지 돌려준다."""
        self.calls.append(allowed)
        return [(i, 1.0) for i in self.ids if allowed is None or i in allowed][
            :k
        ]


def fixed_route(q, s):
    """도구 테스트만 위한 고정 라우팅. 제품 런타임에는 규칙 라우터가 없다."""
    import re

    from legacy_rules import FINANCE_RE, FUTURE_RE, OUT_OF_SCOPE_RE

    if re.search(OUT_OF_SCOPE_RE, q) or re.search(FUTURE_RE, q):
        return "out_of_scope", "고정 응답"
    if not (s.kinds or s.product_ids or re.search(FINANCE_RE, q)):
        return "chat", "고정 응답"
    if s.compare or s.calc or s.goal or (s.amount_won and s.term_months):
        return "hard", "고정 응답"
    return "easy", "고정 응답"


def fake_conditions(q):
    """가짜 라우터가 실모델 대신 채우는 실행 조건(제품 런타임은 LLM 출력만 쓴다)."""
    import re

    m = re.search(r"(\d+)\s*개(?!월)", q)
    return {
        "repayment": "bullet"
        if "만기일시" in q
        else "equal_payment"
        if "원리금균등" in q
        else "equal_principal"
        if "원금균등" in q
        else None,
        "monthly_payout": bool(
            re.search(r"(?:매월|매달|월별)\s*(?:받[는을]\s*)?이자", q)
        ),
        "docs": bool(
            re.search(
                r"우대|조건|가입|대상|수수료|연체|한도|해지|유의|방법|언제|누가",
                q,
            )
        ),
        "count": int(m.group(1)) if m else None,
        "exclude_previous": False,
    }


class FakeLLM:
    """structured 응답을 순서대로 돌려준다(dict -> 출력 모델로 검증). 받은 입력 모델도 기록한다."""

    def __init__(self, *replies):
        """순서대로 돌려줄 응답들을 받는다."""
        self.replies, self.inputs = list(replies), []

    def structured(self, system, payload, out):
        """입력을 기록하고 다음 고정 응답을 출력 모델로 검증해 돌려준다."""
        import re

        from legacy_rules import FUTURE_RE, extract

        from finagent.schemas import RouteOutput

        if out is RouteOutput:
            # 도구 테스트용 고정 응답이다. 실모델 분류 품질은 별도 실제 평가에서 측정한다.
            from pathlib import Path

            from finagent.parser import parse_all
            from finagent.schemas import RouteFilters

            if not hasattr(FakeLLM, "catalog"):
                FakeLLM.catalog = Catalog(
                    CAT.products
                    + parse_all(
                        Path(__file__).resolve().parents[1] / "data/raw"
                    )
                )
            q = payload.question
            previous = payload.previous_conditions or {}
            current = extract(q, FakeLLM.catalog)
            conditions = current.to_dict()
            rewrite = None
            if (
                self.replies
                and "follow_up" in self.replies[0]
                and "operation" not in self.replies[0]
            ):
                rewrite = self.replies.pop(0)
                current = extract(rewrite["question"], FakeLLM.catalog)
                conditions = current.to_dict()
            follow = bool(previous) and (
                rewrite is not None
                or q.startswith(("그거", "그럼", "그 상품", "나은행"))
            )
            if follow:
                changed = bool(
                    conditions["banks"]
                    and conditions["banks"] != previous.get("banks")
                )
                for key in (
                    "kinds",
                    "group",
                    "term_months",
                    "credit_score",
                    "metric",
                ):
                    if conditions[key] in (None, []):
                        conditions[key] = previous.get(key)
                conditions["filters"] = {
                    **previous.get("filters", {}),
                    **conditions["filters"],
                }
                if not conditions["product_ids"] and not changed:
                    conditions["products"] = previous.get("products", [])
                if previous.get("amount_won"):
                    conditions["amount_won"] = (
                        conditions["amount_won"] or previous["amount_won"]
                    )
                    conditions["monthly"] = conditions[
                        "monthly"
                    ] or previous.get("monthly", False)
                    conditions["calc"] = True
            recall = q in {
                "내가 방금 뭐라고 질문했어?",
                "내 이전 질문 다시 알려줘",
                "내가 처음 물어본 상품 이름이 뭐였지?",
                "내가 아까 알려준 신용점수가 몇 점이었지?",
            }
            route = fixed_route(q, current)[0] or "easy"
            if follow and (conditions.get("calc") or conditions.get("compare")):
                route = "hard"
            decision = {
                "ok": True,
                "route": "recall" if recall else route,
                "reason": "테스트용 고정 분류",
            }
            if self.replies and (
                "route" in self.replies[0] or "operation" in self.replies[0]
            ):
                decision = {**self.replies.pop(0)}
            if "route" in decision:
                decision["operation"] = {
                    "chat": "chat",
                    "recall": "recall",
                    "easy": "search_documents",
                    "hard": "calculate"
                    if conditions.get("calc") and not conditions.get("compare")
                    else "query_rates",
                    "out_of_scope": "refuse",
                }[decision.pop("route")]
            decision.pop("source", None)
            decision.setdefault(
                "comparison_scope",
                "market" if conditions.get("compare") else "none",
            )
            decision.setdefault("rank", None)
            decision.setdefault("follow_up", follow)
            decision.setdefault("clarify", None)
            products = conditions.pop("products", None)
            ids = conditions.pop("product_ids", [])
            conditions["products"] = (
                products
                if products is not None
                else [FakeLLM.catalog.by_id[i].name for i in ids]
            )
            goal, calc = (
                conditions.pop("goal", False),
                conditions.pop("calc", False),
            )
            conditions["calculation"] = (
                "goal" if goal else "forward" if calc else "none"
            )
            for key in ("rank", "compare", "question", "exclude_ids"):
                conditions.pop(key, None)
            conditions["filters"] = {
                key: conditions["filters"].get(key)
                for key in RouteFilters.model_fields
            }
            if conditions["metric"] == "max" and any(
                k in ("saving", "deposit") for k in conditions["kinds"]
            ):
                conditions["metric"] = "rate_max"
            conditions.update(fake_conditions(q))
            if conditions["metric"] == "rate_max" and not re.search(
                r"최고|최대\s*금리", q
            ):
                conditions["metric"] = (
                    None  # 우대 설명 질문은 수치 조회가 아니다
                )
            refuse = decision["operation"] == "refuse"
            decision.setdefault(
                "refusal",
                ("future_rate" if re.search(FUTURE_RE, q) else "unsupported")
                if refuse
                else "none",
            )
            decision.setdefault("conditions", conditions)
            return out.model_validate(decision), {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "usd": 0.0,
            }
        self.inputs.append(payload)
        return out.model_validate(self.replies.pop(0)), {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "usd": 0.001,
        }

    def chat(self, system, user, schema=None):
        """다음 고정 응답 텍스트를 돌려준다."""
        return self.replies.pop(0), {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "usd": 0.001,
        }


def ans(reply, ok=True, cites=(), missing=""):
    """AnswerOutput 형식의 고정 응답 dict."""
    return {
        "ok": ok,
        "reply": reply,
        "citations": list(cites),
        "missing": missing,
    }


def route_reply(
    operation="search_documents",
    *,
    scope="none",
    rank=None,
    follow_up=False,
    clarify=None,
    **conditions,
):
    from finagent.schemas import RouteFilters

    return {
        "ok": True,
        "operation": operation,
        "reason": "고정 LLM 응답",
        "comparison_scope": scope,
        "rank": rank,
        "follow_up": follow_up,
        "refusal": "none",
        "clarify": clarify,
        "conditions": {
            "kinds": [],
            "group": None,
            "banks": [],
            "products": [],
            "metric": None,
            "amount_won": None,
            "monthly": False,
            "term_months": None,
            "credit_score": None,
            "rate_pct": None,
            "calculation": "none",
            "repayment": None,
            "monthly_payout": False,
            "docs": False,
            "count": None,
            "exclude_previous": False,
            "filters": {k: None for k in RouteFilters.model_fields},
            **conditions,
        },
    }


def run(q, llm, mcp=None, ids=(A,), prev=None):
    """가짜 의존성으로 v2 그래프를 한 번 실행한다. (final, state)를 돌려준다."""
    deps = Deps(
        llm=llm,
        mcp=mcp or FakeMCP(),
        retriever=FakeRetriever(list(ids)),
        catalog=CAT,
        data_month="202609",
        planner="rule",
        rag_agent="fixed",
    )
    return asyncio.run(run_v2(build_graph(deps), deps, q, prev_slots=prev))


def types(state):
    """상태에 쌓인 이벤트 type 목록."""
    return [e["type"] for e in state["events"]]


def test_chat_route_answers_without_retrieval():
    """잡담은 검색 없이 LLM 한 번으로 답한다."""
    final, st = run(
        "안녕! 반가워", FakeLLM({"ok": True, "reply": "안녕하세요!"})
    )
    assert (
        final["route"] == "chat"
        and final["reply"] == "안녕하세요!"
        and types(st) == ["RouteDecided"]
    )
    assert final["metadata"]["llm_calls"] == 2 and final["warnings"][
        0
    ].startswith("2026년 9월")


def test_out_of_scope_refuses_without_llm():
    """범위 밖 질문은 LLM 없이 거절한다."""
    final, st = run("테슬라 주식 어때?", FakeLLM())
    assert final["route"] == "out_of_scope" and st["llm_calls"] == 1


def test_easy_path_uses_rag_subgraph_and_structured_input():
    """easy 경로는 rag 서브그래프를 거쳐 구조화된 근거로 답한다."""
    llm = FakeLLM(
        ans(
            "가나적금 급여 이체 우대는 연 0.5%p예요",
            cites=[
                {
                    "product_id": A,
                    "section": "우대조건",
                    "quote": "급여 이체 시 연 0.5%p",
                }
            ],
        )
    )
    final, st = run("가나적금 우대조건 뭐야?", llm)
    assert types(st) == ["RouteDecided", "TaskCompleted", "Validation"]
    assert (
        final["citations"][0]["quote"] == "급여 이체 시 연 0.5%p"
        and final["products"][0]["id"] == A
    )
    doc = llm.inputs[0].documents[
        0
    ]  # LLM에는 표와 인용 가능한 설명이 분리돼 들어간다
    assert (
        "|" in doc.rate_table
        and doc.descriptions[0].section == "우대조건"
        and llm.inputs[0].citable_ids == [A]
    )


def test_bad_citation_is_dropped_with_warning_not_rejected():
    """원문과 다른 인용은 답을 반려하지 않고 빼며 경고로 남긴다."""
    bad = ans(
        "최고금리 3.85%예요",
        cites=[
            {"product_id": A, "section": "금리", "quote": "| 12 | 3.85 |"},
            {
                "product_id": A,
                "section": "우대조건",
                "quote": "급여 이체 시 연 0.5%p",
            },
        ],
    )
    final, st = run("가나적금 최고금리 얼마야?", FakeLLM(bad))
    assert st["verified"] and [
        c["quote"] for c in final["citations"] if c["section"] != "금리표"
    ] == ["급여 이체 시 연 0.5%p"]
    assert any("인용 1건은 제외" in w for w in final["warnings"])


def test_wrong_number_is_rejected_then_fallback():
    """근거에 없는 수치는 반려되고, 재작성 한도 후 도구 결과로 대체된다."""
    bad = ans("최고금리 9.99%예요")
    final, st = run("가나적금 최고금리 얼마야?", FakeLLM(bad, bad))
    assert [
        e["verdict"] for e in st["events"] if e["type"] == "Validation"
    ] == ["fail", "fail"]  # 재작성 1회 후 중단
    assert "3.85%" in final["reply"] and any(
        "도구 결과" in w for w in final["warnings"]
    )


def test_insufficient_evidence_stops_without_rewrite():
    """LLM이 근거 부족이라고 하면 재작성 없이 멈춘다."""
    final, st = run(
        "가나적금 중도해지하면 어떻게 돼?",
        FakeLLM(ans("", ok=False, missing="중도해지 조건이 공시에 없어요")),
    )
    assert (
        st["llm_calls"] == 2
        and "중도해지 조건이 공시에 없어요" in final["reply"]
    )


def test_hard_calc_agent_resolves_rate_itself():
    """calc 서브에이전트는 금리가 없으면 스스로 조회해 계산한다."""
    mcp = FakeMCP()
    final, st = run(
        "가나적금 정액적립식 12개월에 월 50만원씩 넣으면 기본금리로 "
        "세전이자 얼마야?",
        FakeLLM(ans("기본금리 2.45%로 세전이자는 79,625원이에요")),
        mcp,
    )
    assert (
        final["route"] == "hard" and final["calculations"][0]["result"] == 79625
    )
    assert [n for n, _ in mcp.calls] == [
        "query_products",
        "calculate",
    ]  # 금리 조회 → 계산 (calc 서브그래프 안에서)
    assert (
        final["products"][0]["id"] == A
        and final["metadata"]["validation_attempts"] == 1
    )


def test_after_tax_answer_satisfies_required_calc():
    """세후 금액만 답해도 필수 계산 결과 검사를 통과한다."""
    final, st = run(
        "가나적금 정액적립식 12개월에 월 50만원씩 넣으면 세후 이자 얼마야?",
        FakeLLM(ans(f"세후 {int(79625 * 0.846):,}원이에요")),
    )
    assert st["verified"] is True


def test_compare_and_calc_run_as_parallel_subagents():
    """비교와 계산 task가 병렬 서브에이전트로 실행된다."""
    mcp = FakeMCP()
    final, st = run(
        "은행 적금 중에 12개월 기본금리 제일 높은 상품에 월 50만원 "
        "넣으면 이자 얼마야?",
        FakeLLM(ans("다라적금이 3.2%로 가장 높고 이자는 104,000원이에요")),
        mcp,
    )
    done = [e["agent"] for e in st["events"] if e["type"] == "TaskCompleted"]
    assert (
        sorted(done) == ["calc", "structured"]
        and final["calculations"][0]["result"] == 104000
    )
    assert final["products"][0]["product"] == "다라적금"


def test_hard_compare_preserves_filters_when_nothing_matches():
    """조회가 0건이면 요청 조건을 보존하고 결과 없음을 알린다."""
    final, st = run(
        "은행 적금 중에 24개월 기본금리 제일 높은 거 뭐야?",
        FakeLLM(),
    )
    assert [
        e["is_sufficient"] for e in st["events"] if e["type"] == "ReplanCheck"
    ] == [False]
    assert not final["products"] and "찾지 못" in final["reply"]
    assert st["tasks"][0]["args"]["term_months"] == 24


def test_compare_answer_picking_non_top_product_is_rejected():
    """비교 질문에 1위가 아닌 상품을 '가장 높다'고 답하면 반려된다(서술 순서는 보지 않는다)."""
    final, st = run(
        "은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?",
        FakeLLM(
            ans("가나적금이 2.45%로 가장 높아요"),
            ans("다라적금이 3.2%로 가장 높아요"),
        ),
    )
    reasons = [e["reason"] for e in st["events"] if e["type"] == "Validation"]
    assert "1위가 아닌 상품을 1위로" in reasons[0] and reasons[1] is None
    assert "다라적금" in reasons[0]


def test_hard_fallback_table_when_validation_keeps_failing():
    """검증이 계속 실패하면 도구 결과 표로 대체한다."""
    bad = ans("다라적금이 9.9%예요")
    final, st = run(
        "은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?",
        FakeLLM(bad, bad, bad),
    )
    assert st["attempts"] == 3 and "3.2%" in final["reply"]
    assert any("검증을 통과하지 못해" in w for w in final["warnings"])


def test_credit_without_score_asks_instead_of_guessing():
    """신용점수 없는 신용대출 비교는 추측하지 않고 점수를 묻는다."""
    final, st = run("신용대출 금리 제일 낮은 곳 어디야?", FakeLLM())
    assert "신용점수" in final["reply"] and st["llm_calls"] == 1


def test_tool_failure_is_reported_not_hidden():
    """도구 실패는 이벤트에 원문으로, 사용자에게는 못 한 작업으로 드러난다."""
    final, st = run(
        "은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?",
        FakeLLM(ans("모르겠어요", ok=False)),
        FakeMCP(fail=True),
    )
    # 원문은 이벤트에, 사용자에게는 무엇을 못 했는지만
    assert any(
        e["type"] == "TaskCompleted"
        and e["ok"] is False
        and "boom" in e["error"]
        for e in st["events"]
    )
    assert any(
        "실패해 이 부분은 답에서 빠졌어요" in w for w in final["warnings"]
    )
    assert not any("boom" in w for w in final["warnings"])


def test_follow_up_inherits_previous_product():
    """후속 질문은 직전 상품을 이어받는다."""
    prev = {"kinds": ["saving"], "product_ids": [A], "banks": [], "group": None}
    final, st = run(
        "그거 우대조건은?", FakeLLM(ans("급여 이체 시 연 0.5%p예요")), prev=prev
    )
    assert final["route"] == "easy" and st["slots"]["product_ids"] == [A]


def test_named_product_skips_retrieval_but_unnamed_question_searches():
    """상품명이 맞은 질문은 검색을 건너뛰고, 이름 없는 질문은 검색한다."""

    def run_with(q):
        """질문을 실행하고 (검색기 호출 기록, 상태)를 돌려준다."""
        r = FakeRetriever([A, B])
        deps = Deps(
            llm=FakeLLM(ans("급여 이체 시 연 0.5%p예요")),
            mcp=FakeMCP(),
            retriever=r,
            catalog=CAT,
            data_month="202609",
            planner="rule",
            rag_agent="fixed",
        )
        _, st = asyncio.run(run_v2(build_graph(deps), deps, q))
        return r.calls, st

    calls, st = run_with("가나적금 우대조건 뭐야?")
    assert calls == [] and st["results"][0]["doc_ids"] == [
        A
    ]  # 이름이 맞은 상품이 그대로 근거
    calls, st = run_with("적금 급여 이체 우대 얼마야?")
    assert calls == [{A, B}] and st["results"][0]["doc_ids"] == [A, B]


def test_graph_drawing_shows_subgraph_internals():
    """그래프 그림(Mermaid)에 서브그래프 내부 노드가 보인다."""
    deps = Deps(
        llm=FakeLLM(),
        mcp=FakeMCP(),
        retriever=FakeRetriever([A]),
        catalog=CAT,
        data_month="202609",
        planner="rule",
        rag_agent="fixed",
    )
    mermaid = build_graph(deps).get_graph(xray=True).draw_mermaid()
    for node in (
        "structured_agent",
        "rag_agent",
        "calc_agent",
        "resolve_rate",
        "replan_check",
    ):
        assert node in mermaid


def test_spread_question_gets_spread_row_and_rejects_max_rate_answer():
    """'우대 몇 %p'에 최고금리(3.85%)로 답하면 반려, 재작성도 실패하면 우대 폭 표로 대체(v2 오답 E2·S4·N10 유형)."""
    llm = FakeLLM(
        ans("가나적금 최고금리는 3.85%예요"), ans("우대 포함 3.85%예요")
    )
    final, st = run("가나적금 우대금리 최고 몇 %p야?", llm)
    assert [t["agent"] for t in st["tasks"]] == ["rag", "structured"]
    row = llm.inputs[0].rows[0]
    assert (row.value, row.unit) == (1.4, "%p") and "1.4%p" in llm.inputs[
        0
    ].to_prompt()
    assert [
        e["verdict"] for e in st["events"] if e["type"] == "Validation"
    ] == ["fail", "fail"]
    assert (
        "우대 폭" in st["events"][-1]["reason"] or "우대 폭" in st["feedback"]
    )
    assert (
        "1.4%p" in final["reply"]
        and final["products"][0]["highlight"]["unit"] == "%p"
    )


def test_spread_answer_passes():
    """우대 폭(%p) 질문에 %p 값부터 답하면 통과한다."""
    final, st = run(
        "가나적금 우대금리 최고 몇 %p야?",
        FakeLLM(ans("가나적금 우대금리는 최고 1.4%p예요 (최고금리 3.85%)")),
    )
    assert st["verified"] and final["reply"].startswith(
        "가나적금 우대금리는 최고 1.4%p"
    )


def test_goal_question_uses_reverse_calculation():
    """목표 금액 질문은 역산(mode=goal) 계산을 쓴다."""
    need = C.saving_goal(10_000_000, 12, 3.0)
    llm = FakeLLM(
        ans(
            f"1년에 1,000만원을 모으려면 연 3.0%로 매달 {need:,}원씩 넣으면 돼요"
        )
    )
    final, st = run(
        "1년 동안 1000만원 모으려면 적금 연 3% 기준 매달 얼마 넣어야 해?", llm
    )
    calc = next(t for t in st["tasks"] if t["agent"] == "calc")
    assert (
        st["route"] == "hard"
        and calc["args"]["mode"] == "goal"
        and calc["args"]["product_type"] == "saving"
    )
    assert st["verified"] and final["calculations"][0]["result"] == need


def test_unnamed_spread_question_uses_top_retrieved_product():
    """이름 없는 우대 폭 질문은 검색 1위 상품으로 조회한다."""
    llm = FakeLLM(ans("다라적금 우대 폭은 최고 0.3%p예요"))
    final, st = run("나은행 환경 적금 우대 최대 몇 %p야?", llm, ids=(B, A))
    spread = [t for t in st["tasks"] if t["agent"] == "structured"]
    assert spread and spread[0]["args"]["product_ids"] == [B] and st["verified"]


# ---------------- LLM Planner (PLANNER=llm) ----------------
def plan(*tasks, ok=True, reason="비교 후 계산"):
    """PlanOutput 형식의 고정 응답 dict."""
    return {
        "ok": ok,
        "tasks": [{"agent": a, "kind": k, "why": "필요"} for a, k in tasks],
        "reason": reason,
    }


def run_llm_planner(q, llm, mcp=None):
    """PLANNER=llm으로 그래프를 실행한다."""
    deps = Deps(
        llm=llm,
        mcp=mcp or FakeMCP(),
        retriever=FakeRetriever([A]),
        catalog=CAT,
        data_month="202609",
        planner="llm",
        rag_agent="fixed",
    )
    return asyncio.run(run_v2(build_graph(deps), deps, q))


def plan_event(st):
    """PlanCreated 이벤트를 찾는다."""
    return next(e for e in st["events"] if e["type"] == "PlanCreated")


def test_llm_planner_decomposes_and_subagents_run():
    """LLM 계획자가 나눈 task대로 서브에이전트가 실행된다."""
    q = (
        "은행 적금 중에 12개월 기본금리 제일 높은 상품에 월 50만원 넣으면 "
        "이자 얼마야?"
    )
    llm = FakeLLM(
        plan(("structured", "saving"), ("calc", "saving")),
        ans("다라적금이 3.2%로 가장 높고 이자는 104,000원이에요"),
    )
    final, st = run_llm_planner(q, llm)
    ev = plan_event(st)
    assert ev["planner"] == "llm" and [t["agent"] for t in ev["tasks"]] == [
        "structured",
        "calc",
    ]
    assert (
        final["calculations"][0]["result"] == 104000
        and st["verified"]
        and st["llm_calls"] == 3
    )
    assert (
        llm.inputs[0].slots["amount_won"] == 500000
    )  # 숫자는 LLM이 아니라 코드 슬롯에서


def test_llm_plan_gate_removes_invalid_tasks():
    """계획 게이트가 질문과 맞지 않거나 중복된 task를 뺀다."""
    q = "은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?"
    llm = FakeLLM(
        plan(
            ("structured", "saving"),
            ("structured", "mortgage"),
            ("calc", "none"),
            ("structured", "saving"),
        ),
        ans("다라적금이 3.2%로 가장 높아요"),
    )
    final, st = run_llm_planner(q, llm)
    ev = plan_event(st)
    assert [t["agent"] for t in ev["tasks"]] == ["structured"] and st[
        "verified"
    ]
    assert ev["removed"] == [
        "structured: 질문에 없는 종류 mortgage",
        "calc: 질문에 금액 없음",
        "structured: 중복",
    ]


def test_llm_planner_refusal_falls_back_to_rule_plan_visibly():
    """LLM 계획자가 거절하면 규칙 계획으로 바꾸고 그 사실을 이벤트에 남긴다."""
    q = "은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?"
    final, st = run_llm_planner(
        q,
        FakeLLM(
            plan(ok=False, reason="모름"), ans("다라적금이 3.2%로 가장 높아요")
        ),
    )
    ev = plan_event(st)
    assert (
        ev["planner"] == "rule"
        and "규칙 계획 사용" in ev["reason"]
        and st["verified"]
    )


def test_rule_planner_is_default_and_calls_no_llm_for_plan():
    """기본은 규칙 계획이며 계획에 LLM을 부르지 않는다."""
    final, st = run(
        "은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?",
        FakeLLM(ans("다라적금이 3.2%로 가장 높아요")),
    )
    assert plan_event(st)["planner"] == "rule" and st["llm_calls"] == 2


# ---------------- 도구 호출 검색 에이전트 (RAG_AGENT=react) ----------------
class ResourceMCP(FakeMCP):
    """product:// Resource 읽기까지 지원하는 가짜 MCP."""

    async def read_resource(self, uri):
        """읽기를 기록하고 테스트 상품 마크다운을 돌려준다."""
        from finagent.parser import to_markdown

        self.calls.append(("read_resource", uri))
        return to_markdown(CAT.by_id[uri.removeprefix("product://")])


class ToolModel(GenericFakeChatModel):
    """정해진 AIMessage(tool_calls 포함)를 순서대로 내는 채팅 모델. create_agent가 부르는 bind_tools는 그대로 둔다."""

    def bind_tools(self, tools, **kwargs):
        """도구 바인딩을 무시하고 자기 자신을 돌려준다."""
        return self


class FakeAgentLLM:
    """정해진 도구 호출 메시지를 순서대로 내는 가짜 에이전트 LLM."""

    def __init__(self, *messages):
        """돌려줄 AIMessage들로 가짜 채팅 모델을 만든다."""
        self.client = ToolModel(messages=iter(messages))

    def usage(self, message):
        """AIMessage 사용량을 structured()와 같은 형식으로 바꾼다."""
        u = message.usage_metadata
        return {
            "prompt_tokens": u["input_tokens"],
            "completion_tokens": u["output_tokens"],
            "usd": 0.001,
        }


def call(name, n=0, **args):
    """도구 호출 하나를 담은 AIMessage를 만든다."""
    return AIMessage(
        "",
        tool_calls=[{"name": name, "args": args, "id": f"c{n}"}],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    )


def run_react(q, agent, llm, ids=(B, A)):
    """RAG_AGENT=react로 그래프를 실행한다. (final, state, mcp)를 돌려준다."""
    mcp = ResourceMCP()
    deps = Deps(
        llm=llm,
        mcp=mcp,
        retriever=FakeRetriever(list(ids)),
        catalog=CAT,
        data_month="202609",
        planner="rule",
        rag_agent="react",
        agent_llm=agent,
    )
    final, st = asyncio.run(run_v2(build_graph(deps), deps, q))
    return final, st, mcp


def test_react_rag_reads_resource_then_selects_evidence():
    """ReAct 검색 에이전트가 원문을 읽은 뒤 근거를 고른다."""
    agent = FakeAgentLLM(
        call("read_product", 1, product_id=A),
        call("select_evidence", 2, product_ids=[A], reason="급여 이체 우대"),
    )
    final, st, mcp = run_react(
        "적금 급여 이체 우대 얼마야?",
        agent,
        FakeLLM(ans("가나적금은 급여 이체 시 연 0.5%p예요")),
    )
    rag = next(r for r in st["results"] if r["agent"] == "rag")
    assert (
        rag["doc_ids"] == [A]
        and rag["fallback"] is None
        and [x["action"] for x in rag["steps"]] == ["read", "finish"]
    )
    assert ("read_resource", f"product://{A}") in mcp.calls
    assert (
        st["llm_calls"] == 4 and st["verified"]
    )  # 검색 에이전트 2회 + 답변 1회가 부모 집계에 합쳐진다


def test_react_rag_gate_rejects_unseen_product_and_falls_back_visibly():
    """본 적 없는 상품 선택은 게이트에서 막히고 검색 순서로 대체된 사실이 남는다."""
    agent = FakeAgentLLM(
        call("select_evidence", 1, product_ids=["deposit_없는상품"], reason="?")
    )
    final, st, _ = run_react(
        "적금 급여 이체 우대 얼마야?",
        agent,
        FakeLLM(ans("다라적금은 연 0.5%p예요")),
        ids=(B,),
    )
    rag = next(r for r in st["results"] if r["agent"] == "rag")
    assert rag["doc_ids"] == [B] and rag["fallback"] == "LLM이 고른 근거 없음"


def test_react_rag_stops_at_model_call_limit():
    """ReAct 검색 에이전트는 모델 호출 상한에서 멈춘다."""
    agent = FakeAgentLLM(
        call("search_products", 1, query="급여이체"),
        call("search_products", 2, query="월급"),
        call("search_products", 3, query="연금"),
        call("search_products", 4, query="넘침"),
    )
    final, st, _ = run_react(
        "적금 급여 이체 우대 얼마야?",
        agent,
        FakeLLM(ans("다라적금은 연 0.5%p예요")),
    )
    rag = next(r for r in st["results"] if r["agent"] == "rag")
    assert (
        len(rag["steps"]) == 3
        and rag["fallback"] == "판단 상한 도달"
        and rag["doc_ids"] == [B, A]
    )


def test_evidence_tools_gate_without_any_agent_framework():
    """EvidenceTools 게이트는 에이전트 프레임워크 없이도 동작한다."""
    ev = EvidenceTools(FakeRetriever([B]), ResourceMCP(), CAT)
    assert asyncio.run(ev.search("급여")) == [B]
    with pytest.raises(EvidenceError):
        asyncio.run(ev.read(A))  # 본 적 없는 후보는 읽을 수 없다
    assert "급여 이체" in asyncio.run(ev.read(B)) and ev.select(
        [B, A], "이유"
    ) == [B]


def test_react_rag_skips_llm_for_named_product():
    """상품명이 맞으면 ReAct 검색 에이전트가 LLM을 부르지 않는다."""
    final, st, _ = run_react(
        "가나적금 우대조건 뭐야?",
        FakeAgentLLM(),
        FakeLLM(ans("가나적금 급여 이체 우대는 연 0.5%p예요")),
    )
    assert st["llm_calls"] == 2 and st["results"][0]["doc_ids"] == [A]


def test_query_elapsed_includes_mcp_wait(monkeypatch):
    """조회 시간에 실제 MCP 호출 대기 시간이 포함된다."""
    from types import SimpleNamespace

    from finagent.graph import agents

    clock = [10.0]
    monkeypatch.setattr(agents.time, "perf_counter", lambda: clock[0])

    class TimedMCP:
        async def call_tool(self, name, args):
            clock[0] += 1.0
            return ToolResult(ok=True, tool_name=name, data={"rows": []}), 0

    async def check():
        state = {
            "task": {"id": "t1", "agent": "structured", "round": 0, "args": {}}
        }
        state.update(
            await agents.s_query(state, SimpleNamespace(mcp=TimedMCP()))
        )
        result = await agents.s_check(state, None)
        assert result["events"][0]["elapsed_ms"] == 1000

    asyncio.run(check())


def test_llm_plan_cannot_omit_explicit_ranking_request():
    """계산만 선택한 계획에도 사용자가 요청한 순위 조회가 추가된다."""
    llm = FakeLLM(
        plan(("calc", "saving")),
        ans("다라적금이 3.2%로 가장 높고 이자는 104,000원이에요"),
    )
    final, state = run_llm_planner(
        "은행 적금 중에 12개월 기본금리 제일 높은 상품에 월 50만원 넣으면 이자 얼마야?",
        llm,
    )
    event = plan_event(state)
    assert {t["agent"] for t in event["tasks"]} == {"calc", "structured"}
    assert "필수 작업 추가" in event["reason"]
    assert state["verified"] and final["products"][0]["product"] == "다라적금"
