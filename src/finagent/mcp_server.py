"""finagent MCP 서버 (stdio subprocess로 기동). 베이스 ADR-0002와 같은 경계.

- Tool    : 호출마다 결과가 달라지는 조회·계산 (query_products, calc_*)
- Resource: URI로 식별되는 정적 문서 (product://{product_id} -> 상품 마크다운)
- Prompt  : 비교·계산 질문의 표준 답변 절차 (compare_procedure)
"""
from pathlib import Path

from fastmcp import FastMCP

from finagent import catalog as C
from finagent.parser import KIND_KO, parse_all, to_markdown

ROOT = Path(__file__).resolve().parents[2]
CAT = C.Catalog(parse_all(ROOT / "data/raw"))
mcp = FastMCP(name="finagent-products")


@mcp.tool()
def query_products(kind: str, group: str | None = None, term_months: int | None = None, metric: str | None = None,
                   credit_score: int | None = None, product_ids: list[str] | None = None,
                   banks: list[str] | None = None, filters: dict | None = None, limit: int = 5) -> dict:
    """금융상품을 조건으로 걸러 금리 순으로 정렬한다. 예·적금은 높은 순, 대출은 낮은 순.
    kind: deposit|saving|mortgage|rent|credit, group: bank|savingsbank, metric: rate(기본금리)|rate_max(우대 포함
    최고금리)|min|max(대출), credit은 credit_score 필수. 공시 평균금리는 쓰지 않는다."""
    if kind not in KIND_KO:
        raise ValueError(f"kind는 {list(KIND_KO)} 중 하나여야 합니다: {kind}")
    return {"rows": CAT.query(kind, group=group, term_months=term_months, metric=metric, credit_score=credit_score,
                              product_ids=product_ids, banks=banks, filters=filters, limit=limit)}


@mcp.tool()
def calc_saving_interest(monthly_won: int, term_months: int, rate_pct: float, compound: bool = False) -> dict:
    """적금 세전이자(원). 단리: 월납입 × T(T+1)/2 × 연이율/1200, 원 미만 절사."""
    return {"name": "적금 세전이자", "result": C.saving_interest(monthly_won, term_months, rate_pct, compound),
            "unit": "원", "inputs": {"monthly_won": monthly_won, "term_months": term_months, "rate_pct": rate_pct},
            "formula": "월복리 누적" if compound else "월납입 × T(T+1)/2 × 연이율 / 1200 (원 미만 절사)"}


@mcp.tool()
def calc_deposit_interest(principal_won: int, term_months: int, rate_pct: float, compound: bool = False) -> dict:
    """정기예금 세전이자(원). 단리: 원금 × 연이율 × T/1200, 원 미만 절사."""
    return {"name": "예금 세전이자", "result": C.deposit_interest(principal_won, term_months, rate_pct, compound),
            "unit": "원", "inputs": {"principal_won": principal_won, "term_months": term_months, "rate_pct": rate_pct},
            "formula": "원금 × ((1+연이율/1200)^T - 1)" if compound else "원금 × 연이율 × T / 1200 (원 미만 절사)"}


@mcp.tool()
def calc_loan_payment(principal_won: int, term_months: int, rate_pct: float, method: str = "equal_payment") -> dict:
    """대출 월 상환액(원). method: equal_payment(원리금균등) | bullet(만기일시, 월 이자만)."""
    return {"name": "대출 월 상환액", "result": C.loan_payment(principal_won, term_months, rate_pct, method),
            "unit": "원", "inputs": {"principal_won": principal_won, "term_months": term_months, "rate_pct": rate_pct},
            "formula": "원리금균등: 원금 × r / (1 - (1+r)^-T), r=연이율/1200" if method == "equal_payment"
            else "만기일시: 원금 × 연이율 / 1200"}


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
1. 순위·금리는 query_products 결과의 value만 사용하세요. 1위가 여러 개면 모두 말하세요.
2. 이자·상환액은 calc_* 결과의 result만 사용하세요. 직접 계산하지 마세요.
3. 우대조건 같은 설명은 검색된 원문 문장을 그대로 인용하세요.
4. 공시 평균금리는 언급하지 마세요. 실제 금리는 개인 조건에 따라 다르다고 덧붙이세요."""


if __name__ == "__main__":
    mcp.run(show_banner=False)
