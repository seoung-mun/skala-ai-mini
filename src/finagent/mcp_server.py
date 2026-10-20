"""finagent MCP 서버 (stdio subprocess로 기동). 베이스 ADR-0002와 같은 경계.

- Tool    : query_products(조회), calculate(계산) — 결과는 항상 ToolResult(ok, tool_name, data, error)
- Resource: product://{product_id} -> 상품 마크다운
- Prompt  : compare_procedure — 비교·계산 질문의 답변 절차

도구는 API 단위가 아니라 에이전트의 목표 단위로 통합한다(ACI): 이자·상환액 계산은 calculate 하나.
"""
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

from finagent import catalog as C
from finagent.parser import KIND_KO, parse_all, to_markdown
from finagent.schemas import Calculation, ToolResult

ROOT = Path(__file__).resolve().parents[2]
CAT = C.Catalog(parse_all(ROOT / "data/raw"))
mcp = FastMCP(name="finagent-products")


def _fail(tool: str, e: Exception) -> dict:
    return ToolResult(ok=False, tool_name=tool, error=str(e)).model_dump()


@mcp.tool()
def query_products(kind: str, group: str | None = None, term_months: int | None = None, metric: str | None = None,
                   credit_score: int | None = None, product_ids: list[str] | None = None,
                   banks: list[str] | None = None, filters: dict | None = None, limit: int = 5) -> dict:
    """금융상품을 조건으로 걸러 금리 순으로 정렬한다. 예·적금은 높은 순, 대출은 낮은 순, 1위 동률은 모두 포함.
    kind: deposit|saving|mortgage|rent|credit, group: bank|savingsbank, metric: rate(기본금리)|rate_max(우대 포함
    최고금리)|spread(우대 폭 %p = 최고 - 기본)|min|max(대출), credit은 credit_score 필수. 공시 평균금리는 쓰지 않는다."""
    try:
        if kind not in KIND_KO:
            raise ValueError(f"kind는 {list(KIND_KO)} 중 하나여야 합니다: {kind}")
        rows = CAT.query(kind, group=group, term_months=term_months, metric=metric, credit_score=credit_score,
                         product_ids=product_ids, banks=banks, filters=filters, limit=limit)
        return ToolResult(ok=True, tool_name="query_products", data={"rows": rows}).model_dump()
    except ValueError as e:
        return _fail("query_products", e)


@mcp.tool()
def calculate(product_type: Literal["saving", "deposit", "loan"], amount_won: int, term_months: int, rate_pct: float,
              interest_type: Literal["simple", "compound"] = "simple",
              repayment: Literal["equal_payment", "bullet"] = "equal_payment",
              mode: Literal["forward", "goal"] = "forward") -> dict:
    """이자·상환액 계산(원). saving=월 납입액 기준 적금 세전/세후 이자, deposit=원금 기준 예금 세전/세후 이자,
    loan=월 상환액(equal_payment 원리금균등 | bullet 만기일시: 월 이자만). 원 미만 절사(대출은 반올림).
    mode=goal이면 역산: amount_won이 saving·deposit은 목표 세후 만기 수령액, loan은 월 상환 가능액이고
    result는 필요한 월 납입액 / 필요한 원금 / 최대 대출 원금이다."""
    try:
        compound = interest_type == "compound"
        if mode == "goal":
            if product_type == "loan":
                result, name = C.loan_capacity(amount_won, term_months, rate_pct, repayment), "최대 대출 원금"
                formula = "월 상환액(" + ("원리금균등" if repayment == "equal_payment" else "만기일시") + ") ≤ 월 상환 가능액인 최대 원금"
            else:
                fn = C.saving_goal if product_type == "saving" else C.deposit_goal
                result = fn(amount_won, term_months, rate_pct, compound)
                name = "목표 달성 월 납입액" if product_type == "saving" else "목표 달성 필요 원금"
                formula = ("월납입 × T" if product_type == "saving" else "원금") + \
                    " + 세후이자(세전 × (1 - 0.154)) ≥ 목표 금액인 최소 금액, 원 단위"
            after = None
        elif product_type == "loan":
            result, after = C.loan_payment(amount_won, term_months, rate_pct, repayment), None
            name = "대출 월 상환액"
            formula = ("원리금균등: 원금 × r / (1 - (1+r)^-T), r=연이율/1200" if repayment == "equal_payment"
                       else "만기일시: 원금 × 연이율 / 1200 (월 이자)")
        else:
            fn = C.saving_interest if product_type == "saving" else C.deposit_interest
            result = fn(amount_won, term_months, rate_pct, compound)
            after = C.after_tax(result)
            name = "적금 이자" if product_type == "saving" else "예금 이자"
            formula = ("월복리" if compound else ("월납입 × T(T+1)/2 × 연이율 / 1200" if product_type == "saving"
                                                 else "원금 × 연이율 × T / 1200")) + ", 원 미만 절사, 세후 = 세전 × (1 - 0.154)"
        calc = Calculation(name=name, result=result, after_tax=after, inputs={
            "product_type": product_type, "amount_won": amount_won, "term_months": term_months,
            "rate_pct": rate_pct, "interest_type": interest_type, "repayment": repayment, "mode": mode}, formula=formula)
        return ToolResult(ok=True, tool_name="calculate", data=calc.model_dump()).model_dump()
    except ValueError as e:
        return _fail("calculate", e)


@mcp.resource("product://{product_id}")
def product_doc(product_id: str) -> str:
    """상품 공시 문서(마크다운)."""
    if product_id not in CAT.by_id:
        raise ValueError(f"없는 상품: {product_id}")
    return to_markdown(CAT.by_id[product_id])


@mcp.prompt()
def compare_procedure(question: str) -> str:
    """비교·계산 질문의 답변 절차."""
    return f"""다음 질문에 금융상품 공시 데이터로 답하세요: {question}

절차 (반드시 지킬 것)
1. 순위·금리는 rows의 value만 사용하세요. 1위가 여러 개면 모두 말하고, 1위부터 답하세요.
2. 이자·상환액은 calculations의 result(세전)·after_tax(세후)만 사용하세요. 직접 계산하지 마세요.
3. 우대조건 같은 설명은 documents.descriptions의 text를 그대로 인용하세요. 금리표는 인용하지 마세요.
4. 공시 평균금리는 언급하지 마세요. 근거가 부족하면 ok=false로 답하세요."""


if __name__ == "__main__":
    mcp.run(show_banner=False)
