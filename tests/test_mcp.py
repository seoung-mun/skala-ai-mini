"""MCP 서버를 실제 subprocess(stdio)로 띄워 Tool/Resource/Prompt를 호출한다."""
import asyncio
from pathlib import Path

import pytest

from finagent.mcp_client import MCPClient, ToolFailed, with_retry

HAS_RAW = any((Path(__file__).resolve().parents[1] / "data/raw").glob("*.json"))


@pytest.mark.skipif(not HAS_RAW, reason="data/raw 없음")
def test_mcp_tools_resource_prompt_end_to_end():
    async def run():
        c = MCPClient()
        await c.start()
        try:
            calc, retries = await c.call_tool("calc_saving_interest",
                                              {"monthly_won": 500000, "term_months": 12, "rate_pct": 2.45})
            assert calc["result"] == 79625 and retries == 0
            rows, _ = await c.call_tool("query_products", {"kind": "saving", "group": "bank", "term_months": 12,
                                                           "metric": "rate", "limit": 1})
            assert rows["rows"] and all(r["value"] == rows["rows"][0]["value"] for r in rows["rows"])
            empty, _ = await c.call_tool("query_products", {"kind": "saving", "term_months": 999})
            assert empty == {"rows": []}
            doc = await c.read_resource(f"product://{rows['rows'][0]['product_id']}")
            assert doc.startswith("---") and "## 금리" in doc
            assert "query_products" in await c.get_prompt("compare_procedure", {"question": "q"})
            with pytest.raises(ToolFailed, match="kind"):
                await c.call_tool("query_products", {"kind": "stock"})
            with pytest.raises(ToolFailed, match="신용점수"):
                await c.call_tool("query_products", {"kind": "credit"})
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
