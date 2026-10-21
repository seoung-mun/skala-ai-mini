"""MCP 서버를 실제 subprocess(stdio)로 띄워 Tool/Resource/Prompt를 호출한다."""

import asyncio
from pathlib import Path

import pytest

from finagent.mcp_client import MCPClient, with_retry

HAS_RAW = any((Path(__file__).resolve().parents[1] / "data/raw").glob("*.json"))


@pytest.mark.skipif(not HAS_RAW, reason="data/raw 없음")
def test_mcp_tools_resource_prompt_end_to_end():
    """실제 MCP 서버로 Tool·Resource·Prompt를 끝까지 호출한다."""

    async def run():
        """클라이언트를 띄워 도구·Resource·Prompt를 차례로 호출한다."""
        c = MCPClient()
        await c.start()
        try:
            calc, retries = await c.call_tool(
                "calculate",
                {
                    "product_type": "saving",
                    "amount_won": 500000,
                    "term_months": 12,
                    "rate_pct": 2.45,
                },
            )
            assert (
                calc.ok
                and calc.data["result"] == 79625
                and calc.data["after_tax"] == 67362
                and retries == 0
            )
            loan, _ = await c.call_tool(
                "calculate",
                {
                    "product_type": "loan",
                    "amount_won": 100_000_000,
                    "term_months": 360,
                    "rate_pct": 4.5,
                },
            )
            assert (
                loan.data["result"] == 506685 and loan.data["after_tax"] is None
            )
            cap, _ = await c.call_tool(
                "calculate",
                {
                    "product_type": "loan",
                    "amount_won": 506685,
                    "term_months": 360,
                    "rate_pct": 4.5,
                    "mode": "goal",
                },
            )
            assert (
                cap.ok
                and cap.data["name"] == "최대 대출 원금"
                and 99_999_000 < cap.data["result"] < 100_001_000
            )
            rows, _ = await c.call_tool(
                "query_products",
                {
                    "kind": "saving",
                    "group": "bank",
                    "term_months": 12,
                    "metric": "rate",
                    "limit": 1,
                },
            )
            top = rows.data["rows"]
            assert (
                rows.ok
                and top
                and all(r["value"] == top[0]["value"] for r in top)
            )
            empty, _ = await c.call_tool(
                "query_products", {"kind": "saving", "term_months": 999}
            )
            assert empty.ok and empty.data == {"rows": []}
            doc = await c.read_resource(f"product://{top[0]['product_id']}")
            assert doc.startswith("---") and "## 금리" in doc
            assert "질문: q" in await c.get_prompt(
                "compare_procedure", {"question": "q"}
            )
            bad_kind, _ = await c.call_tool("query_products", {"kind": "stock"})
            assert not bad_kind.ok and "kind" in bad_kind.error
            no_score, _ = await c.call_tool(
                "query_products", {"kind": "credit"}
            )
            assert not no_score.ok and "신용점수" in no_score.error
            bad_type, _ = await c.call_tool(
                "calculate",
                {
                    "product_type": "stock",
                    "amount_won": 1,
                    "term_months": 1,
                    "rate_pct": 1,
                },
            )
            assert (
                not bad_type.ok and bad_type.tool_name == "calculate"
            )  # 스키마 위반도 ok=false로 돌아온다
        finally:
            await c.stop()

    asyncio.run(run())


@pytest.mark.skipif(not HAS_RAW, reason="data/raw 없음")
def test_mcp_concurrent_calls_on_one_session():
    """Send 병렬 서브에이전트처럼 한 세션에서 동시에 호출해도 응답이 요청과 바르게 짝지어진다."""

    async def run():
        """한 세션에서 계산 도구를 동시에 여러 번 호출한다."""
        c = MCPClient()
        await c.start()
        try:
            amounts = [100_000 * i for i in range(1, 9)]
            outs = await asyncio.gather(
                *(
                    c.call_tool(
                        "calculate",
                        {
                            "product_type": "deposit",
                            "amount_won": a,
                            "term_months": 12,
                            "rate_pct": 3.0,
                        },
                    )
                    for a in amounts
                )
            )
            assert [r.data["inputs"]["amount_won"] for r, _ in outs] == amounts
            assert all(
                r.ok and r.data["result"] == a * 3 // 100
                for (r, _), a in zip(outs, amounts)
            )
        finally:
            await c.stop()

    asyncio.run(run())


def test_with_retry_retries_only_timeouts():
    """with_retry는 타임아웃만 재시도하고 다른 오류는 바로 올린다."""
    calls = []

    async def flaky():
        """첫 호출만 타임아웃을 낸다."""
        calls.append(1)
        if len(calls) < 2:
            raise asyncio.TimeoutError
        return "ok"

    async def broken():
        """항상 영구 오류를 낸다."""
        raise ValueError("permanent")

    assert asyncio.run(with_retry(flaky, base_delay=0)) == ("ok", 1)
    with pytest.raises(ValueError):
        asyncio.run(with_retry(broken, base_delay=0))
