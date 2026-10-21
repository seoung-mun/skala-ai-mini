"""금리 숫자와 함께 공시 옵션을 계산까지 보존한다."""

import asyncio
from types import SimpleNamespace

import pytest

from finagent.catalog import Catalog
from finagent.graph.agents import c_calculate, c_check, c_resolve
from finagent.parser import Product
from finagent.schemas import ToolResult


def catalog_for(kind="deposit", *, rate_type="복리", repayment=None):
    option = (
        {
            "term_months": 12,
            "rate_type_nm": rate_type,
            "rate": 3.6,
            "rate_max": 4.0,
            "spread": 0.4,
        }
        if kind == "deposit"
        else {
            "rpay_type_nm": repayment,
            "lend_rate_type_nm": "고정금리",
            "min": 3.6,
            "max": 5.0,
            "avg": 7.0,
        }
    )
    return Catalog(
        [
            Product(
                kind=kind,
                group="bank",
                key=("202609", "bank", "product"),
                base={"kor_co_nm": "검증은행", "fin_prdt_nm": "검증상품"},
                options=[option],
                flags=[],
            )
        ]
    )


class CatalogTools:
    def __init__(self, catalog):
        self.catalog = catalog
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "query_products":
            return ToolResult(
                ok=True,
                tool_name=name,
                data={"rows": self.catalog.query(**args)},
            ), 0
        from finagent.mcp_server import calculate

        return ToolResult(**calculate.fn(**args)), 0


async def resolve(catalog, *, metric=None, **overrides):
    kind = catalog.products[0].kind
    args = {
        "product_type": "deposit" if kind == "deposit" else "loan",
        "amount_won": 10_000_000,
        "term_months": 12,
        "rate_pct": None,
        "mode": "forward",
        **overrides,
    }
    task = {
        "args": args,
        "rate_query": {"kind": kind, **({"metric": metric} if metric else {})},
    }
    deps = SimpleNamespace(mcp=CatalogTools(catalog))
    work = (await c_resolve({"task": task}, deps))["work"]
    return work, deps


def test_compound_option_reaches_the_actual_calculation_tool():
    async def run():
        work, deps = await resolve(catalog_for())
        assert work["args"]["interest_type"] == "compound"
        result = (await c_calculate({"work": work}, deps))["work"]["res"]
        assert result["ok"]
        assert result["data"]["result"] == 365999
        assert result["data"]["inputs"]["interest_type"] == "compound"

    asyncio.run(run())


def test_spread_is_never_used_as_an_annual_interest_rate():
    async def run():
        work, deps = await resolve(catalog_for(), metric="spread")
        assert "우대 폭" in work["error"]
        assert await c_calculate({"work": work}, deps) == {}
        assert [name for name, _ in deps.mcp.calls] == ["query_products"]

    asyncio.run(run())


def test_explicit_simple_interest_cannot_override_a_compound_product():
    work, _ = asyncio.run(resolve(catalog_for(), interest_type="simple"))
    assert "충돌" in work["error"]


def test_query_preserves_an_explicit_interest_type_filter():
    catalog = catalog_for()
    catalog.products[0].options.append(
        {**catalog.products[0].options[0], "rate_type_nm": "단리", "rate": 4.0}
    )
    rows = catalog.query("deposit", filters={"rate_type_nm": "복리"})
    assert rows[0]["value"] == 3.6
    assert rows[0]["interest_type"] == "compound"


def test_unknown_disclosed_interest_type_does_not_default_to_simple():
    work, _ = asyncio.run(resolve(catalog_for(rate_type="")))
    assert "이자 방식" in work["error"]


def test_bullet_repayment_option_reaches_the_actual_calculation_tool():
    async def run():
        work, deps = await resolve(
            catalog_for("rent", repayment="만기일시상환방식")
        )
        assert work["args"]["repayment"] == "bullet"
        result = (await c_calculate({"work": work}, deps))["work"]["res"]
        assert result["data"]["result"] == 30000
        assert result["data"]["inputs"]["repayment"] == "bullet"

    asyncio.run(run())


@pytest.mark.parametrize("repayment", ["분할상환방식", None])
def test_ambiguous_repayment_requires_an_explicit_assumption(repayment):
    work, _ = asyncio.run(resolve(catalog_for("rent", repayment=repayment)))
    assert "상환방식" in work["error"]
    result = asyncio.run(
        c_check(
            {"work": work, "task": {"id": "c1", "agent": "calc", "round": 0}},
            None,
        )
    )["results"][0]
    assert result["ok"] is False
    assert result["clarify"] == work["error"]
    work, _ = asyncio.run(
        resolve(
            catalog_for("rent", repayment=repayment),
            repayment="equal_payment",
        )
    )
    assert "error" not in work
    assert work["args"]["repayment"] == "equal_payment"


@pytest.mark.parametrize(
    ("source", "requested"),
    [("만기일시상환방식", "equal_payment"), ("분할상환방식", "bullet")],
)
def test_explicit_repayment_cannot_conflict_with_disclosed_option(
    source, requested
):
    work, _ = asyncio.run(
        resolve(catalog_for("rent", repayment=source), repayment=requested)
    )
    assert "충돌" in work["error"]


def test_explicit_rate_calculation_keeps_its_existing_assumption_path():
    work, deps = asyncio.run(resolve(catalog_for(), rate_pct=4.0))
    assert work["args"]["rate_pct"] == 4.0
    assert work["rate_from"] is None
    assert not deps.mcp.calls
