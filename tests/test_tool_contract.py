"""도구 계약: 해당 없는 인자·잘못된 이름은 조용히 0건이 아니라 이유가 있는 오류, 설명은 스키마에 짧게."""

import asyncio
from pathlib import Path

import pytest
from test_graph import CAT, FakeLLM, ans, run

from finagent.schemas import AnswerInput, Calculation
from finagent.validate import check_answer

HAS_RAW = any((Path(__file__).resolve().parents[1] / "data/raw").glob("*.json"))


@pytest.mark.parametrize(
    "kwargs,msg",
    [
        ({"limit": 0}, "limit"),
        ({"filters": {"mrtg_type_nm": "아파트"}}, "filters 키"),
        ({"banks": ["가"]}, "공시에 없는 금융회사명"),
        ({"product_ids": ["nope"]}, "product_id"),
        ({"credit_score": 700}, "credit_score"),
    ],
)
def test_query_rejects_inapplicable_or_unknown_args(kwargs, msg):
    """조회는 해당 없는 인자·모르는 값을 이유가 있는 오류로 거부한다."""
    with pytest.raises(ValueError, match=msg):
        CAT.query("saving", **kwargs)


def test_query_rejects_term_for_loans():
    """대출 조회에 기간(term_months)을 넣으면 거부한다."""
    with pytest.raises(ValueError, match="term_months"):
        CAT.query("mortgage", term_months=360)


def test_plan_limit_ignores_months_and_sends_only_applicable_args():
    """'12개월'을 개수로 읽지 않고, 종류에 맞는 인자만 보낸다."""
    llm = FakeLLM(ans("나은행 다라적금이 3.2%로 가장 높아요"))
    _, st = run("은행 적금 12개월 기본금리 제일 높은 거 뭐야?", llm)
    args = next(t["args"] for t in st["tasks"] if t["agent"] == "structured")
    assert (
        args["limit"] == 5
        and args["term_months"] == 12
        and "credit_score" not in args
    )


def test_answer_input_shows_calc_premise_and_failed_tasks():
    """답변 입력에 계산 전제와 실패한 작업이 보인다."""
    text = AnswerInput(
        question="q",
        failed=["적금 기본금리 조회"],
        calculations=[
            Calculation(
                name="적금 이자",
                result=79625,
                after_tax=67362,
                formula="f",
                inputs={
                    "product_type": "saving",
                    "amount_won": 500000,
                    "term_months": 12,
                    "rate_pct": 2.45,
                    "mode": "forward",
                    "interest_type": "simple",
                },
            )
        ],
    ).to_prompt()
    assert (
        "세전 79,625원, 세후 67,362원 (전제: 월 납입 500,000원 · 12개월 "
        "· 연 2.45% · 단리" in text
    )
    assert "## 수행하지 못한 조사\n- 적금 기본금리 조회" in text


def test_check_answer_reports_every_reason_at_once():
    """check_answer는 반려 사유를 한 번에 모두 알려준다."""
    ok, why = check_answer(
        "나은행이 9.9%로 가장 높아요",
        {"pct": {3.2, 2.45}, "won": set()},
        [],
        {},
        winner=([["가은행"]], ["가은행", "나은행"]),
    )
    assert (
        not ok
        and why.startswith("1. 근거에 없는 수치")
        and "2. 조회 1위 가은행" in why
        and "3. '나은행이 9.9%로 가장 높아요'에서 1위가 아닌" in why
    )


@pytest.mark.skipif(not HAS_RAW, reason="data/raw 없음")
def test_mcp_tool_schema_is_compact_and_annotated():
    """MCP 도구 설명은 짧고, 인자별 설명과 읽기 전용 표시가 있다."""
    from finagent import mcp_server as M

    tools = asyncio.run(M.mcp.get_tools())
    for name, arg in (
        ("query_products", "filters"),
        ("calculate", "amount_won"),
    ):
        t = tools[name].to_mcp_tool()
        assert (
            t.annotations.readOnlyHint and len(t.description) < 200
        )  # 의미는 인자별 description에
        assert t.inputSchema["properties"][arg]["description"]
    calc = M.calculate.fn
    assert (
        "interest_type"
        in calc("loan", 1_000_000, 12, 4.0, interest_type="compound")["error"]
    )
    assert (
        "repayment"
        in calc("saving", 100_000, 12, 3.0, repayment="bullet")["error"]
    )
    assert "repayment" not in calc("saving", 100_000, 12, 3.0)["data"]["inputs"]
    assert (
        "공시에 없는"
        in M.query_products.fn("deposit", banks=["카카오뱅크"])["error"]
    )
