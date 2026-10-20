"""MCP 서버를 실제 subprocess(stdio)로 띄워 Tool/Resource/Prompt를 호출한다."""
import asyncio
from pathlib import Path

import pytest

from finagent.mcp_client import MCPClient, with_retry

HAS_RAW = any((Path(__file__).resolve().parents[1] / "data/raw").glob("*.json"))


@pytest.mark.skipif(not HAS_RAW, reason="data/raw 없음")
def test_mcp_tools_resource_prompt_end_to_end():
    async def run():
        c = MCPClient()
        await c.start()
        try:
            calc, retries = await c.call_tool("calculate", {"product_type": "saving", "amount_won": 500000,
                                                            "term_months": 12, "rate_pct": 2.45})
            assert calc.ok and calc.data["result"] == 79625 and calc.data["after_tax"] == 67362 and retries == 0
            loan, _ = await c.call_tool("calculate", {"product_type": "loan", "amount_won": 100_000_000,
                                                      "term_months": 360, "rate_pct": 4.5})
            assert loan.data["result"] == 506685 and loan.data["after_tax"] is None
            rows, _ = await c.call_tool("query_products", {"kind": "saving", "group": "bank", "term_months": 12,
                                                           "metric": "rate", "limit": 1})
            top = rows.data["rows"]
            assert rows.ok and top and all(r["value"] == top[0]["value"] for r in top)
            empty, _ = await c.call_tool("query_products", {"kind": "saving", "term_months": 999})
            assert empty.ok and empty.data == {"rows": []}
            doc = await c.read_resource(f"product://{top[0]['product_id']}")
            assert doc.startswith("---") and "## 금리" in doc
            assert "rows" in await c.get_prompt("compare_procedure", {"question": "q"})
            bad_kind, _ = await c.call_tool("query_products", {"kind": "stock"})
            assert not bad_kind.ok and "kind" in bad_kind.error
            no_score, _ = await c.call_tool("query_products", {"kind": "credit"})
            assert not no_score.ok and "신용점수" in no_score.error
            bad_type, _ = await c.call_tool("calculate", {"product_type": "stock", "amount_won": 1,
                                                          "term_months": 1, "rate_pct": 1})
            assert not bad_type.ok and bad_type.tool_name == "calculate"  # 스키마 위반도 ok=false로 돌아온다
        finally:
            await c.stop()
    asyncio.run(run())


def test_with_retry_retries_only_timeouts():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise asyncio.TimeoutError
        return "ok"

    async def broken():
        raise ValueError("permanent")

    assert asyncio.run(with_retry(flaky, base_delay=0)) == ("ok", 1)
    with pytest.raises(ValueError):
        asyncio.run(with_retry(broken, base_delay=0))
