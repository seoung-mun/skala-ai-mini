"""finagent MCP 서버 (stdio subprocess로 기동).

- Tool    : query_products(조회), calculate(계산) — 결과는 항상 ToolResult(ok, tool_name, data, error)
- Resource: product://{product_id} -> 상품 마크다운
- Prompt  : compare_procedure — 비교·계산 질문의 답변 절차

도구는 API 단위가 아니라 에이전트의 목표 단위로 통합한다(ACI): 이자·상환액 계산은 calculate 하나.
"""

from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from finagent import catalog as C
from finagent.parser import parse_all, to_markdown
from finagent.schemas import Calculation, ToolResult

ROOT = Path(__file__).resolve().parents[2]
CAT = C.Catalog(parse_all(ROOT / "data/raw"))
mcp = FastMCP(name="finagent-products")
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, idempotentHint=True, openWorldHint=False
)
Kind = Literal["deposit", "saving", "mortgage", "rent", "credit"]


def _fail(tool: str, e: Exception) -> dict:
    """예외를 실패 ToolResult(dict)로 바꾼다."""
    return ToolResult(ok=False, tool_name=tool, error=str(e)).model_dump()


@mcp.tool(
    title="금융상품 금리 조회",
    annotations=READ_ONLY,
    description="조건에 맞는 상품을 상품당 최적 옵션 1행씩 금리 순으로 "
    "돌려준다(예·적금 높은 순, 대출 낮은 순, "
    "limit 경계 동률 포함). 공시 평균금리는 쓰지 않는다. data.rows = "
    "[{product_id, bank, product, "
    "group, kind, metric, value, option}]",
)
def query_products(
    kind: Annotated[
        Kind,
        Field(
            description="deposit=정기예금, saving=적금, mortgage=주담대, "
            "rent=전세대출, credit=신용대출"
        ),
    ],
    group: Annotated[
        Literal["bank", "savingsbank"] | None, Field(description="금융권")
    ] = None,
    term_months: Annotated[
        int | None,
        Field(gt=0, description="예·적금 기간(개월). 대출에는 넣지 않는다"),
    ] = None,
    metric: Annotated[
        Literal["rate", "rate_max", "spread", "min", "max"] | None,
        Field(
            description="예·적금 rate=기본|rate_max=우대 포함 최고|spread=우대 "
            "폭(%p), "
            "주담대·전세 min|max, 신용대출은 비운다"
        ),
    ] = None,
    credit_score: Annotated[
        int | None, Field(ge=0, le=1000, description="신용대출 필수")
    ] = None,
    product_ids: Annotated[
        list[str] | None, Field(description="kind와 같은 종류의 상품 id")
    ] = None,
    banks: Annotated[
        list[str] | None,
        Field(description="공시 금융회사명 그대로(예: '주식회사 카카오뱅크')"),
    ] = None,
    filters: Annotated[
        dict[str, str] | None,
        Field(
            description="옵션 필터. saving: rsrv_type_nm / mortgage: mrtg_type_nm, "
            "rpay_type_nm, "
            "lend_rate_type_nm / rent: rpay_type_nm, lend_rate_type_nm / "
            "credit: crdt_prdt_type(1 일반|2 마이너스|3 카드론)"
        ),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 5,
) -> dict:
    """catalog.query를 감싼 금리 조회 도구. 모델에 보이는 설명은 description 인자에 있다."""
    try:
        rows = CAT.query(
            kind,
            group=group,
            term_months=term_months,
            metric=metric,
            credit_score=credit_score,
            product_ids=product_ids,
            banks=banks,
            filters=filters,
            limit=limit,
        )
        return ToolResult(
            ok=True, tool_name="query_products", data={"rows": rows}
        ).model_dump()
    except ValueError as e:
        return _fail("query_products", e)


@mcp.tool(
    title="이자·상환액 계산",
    annotations=READ_ONLY,
    description="예·적금 세전/세후 이자, 대출 월 상환액, 목표 금액 역산(원). 원 미만 "
    "절사(대출은 반올림). "
    "data = {name, result, after_tax, unit, inputs, formula}",
)
def calculate(
    product_type: Literal["saving", "deposit", "loan"],
    amount_won: Annotated[
        int,
        Field(
            gt=0,
            description="forward: saving=월 납입액, deposit=원금, loan=대출 원금 / "
            "goal: saving·deposit=목표 세후 수령액, loan=월 상환 가능액",
        ),
    ],
    term_months: Annotated[int, Field(gt=0, description="개월(30년=360)")],
    rate_pct: Annotated[
        float,
        Field(
            ge=0.1,
            le=30,
            description="연이율 %(예: 3.5). 0.035처럼 소수로 넣지 않는다",
        ),
    ],
    interest_type: Annotated[
        Literal["simple", "compound"] | None,
        Field(description="saving·deposit만. 생략=단리, compound=월복리"),
    ] = None,
    repayment: Annotated[
        Literal["equal_payment", "bullet"] | None,
        Field(
            description="loan만. 생략=원리금균등, bullet=만기일시(월 이자만)"
        ),
    ] = None,
    mode: Annotated[
        Literal["forward", "goal"],
        Field(description="goal=목표에서 필요한 금액 역산"),
    ] = "forward",
) -> dict:
    """이자·상환액·목표 역산 계산 도구. 모델에 보이는 설명은 description 인자에 있다."""
    try:
        loan = product_type == "loan"
        if loan and interest_type is not None:
            raise ValueError(
                "interest_type은 saving·deposit에만 적용됩니다(loan은 "
                "repayment를 쓰세요)"
            )
        if not loan and repayment is not None:
            raise ValueError(
                "repayment는 loan에만 적용됩니다(saving·deposit은 "
                "interest_type을 쓰세요)"
            )
        compound, repayment = (
            interest_type == "compound",
            repayment or "equal_payment",
        )
        if mode == "goal":
            if loan:
                result, name = (
                    C.loan_capacity(
                        amount_won, term_months, rate_pct, repayment
                    ),
                    "최대 대출 원금",
                )
                formula = (
                    "월 상환액("
                    + (
                        "원리금균등"
                        if repayment == "equal_payment"
                        else "만기일시"
                    )
                    + ") ≤ 월 상환 가능액인 최대 원금"
                )
            else:
                fn = (
                    C.saving_goal
                    if product_type == "saving"
                    else C.deposit_goal
                )
                result = fn(amount_won, term_months, rate_pct, compound)
                name = (
                    "목표 달성 월 납입액"
                    if product_type == "saving"
                    else "목표 달성 필요 원금"
                )
                formula = (
                    ("월납입 × T" if product_type == "saving" else "원금")
                    + " + 세후이자(세전 × (1 - 0.154)) ≥ 목표 금액인 최소 "
                    "금액, 원 단위"
                )
            after = None
        elif loan:
            result, after = (
                C.loan_payment(amount_won, term_months, rate_pct, repayment),
                None,
            )
            name = "대출 월 상환액"
            formula = (
                "원리금균등: 원금 × r / (1 - (1+r)^-T), r=연이율/1200"
                if repayment == "equal_payment"
                else "만기일시: 원금 × 연이율 / 1200 (월 이자)"
            )
        else:
            fn = (
                C.saving_interest
                if product_type == "saving"
                else C.deposit_interest
            )
            result = fn(amount_won, term_months, rate_pct, compound)
            after = C.after_tax(result)
            name = "적금 이자" if product_type == "saving" else "예금 이자"
            formula = (
                "월복리"
                if compound
                else (
                    "월납입 × T(T+1)/2 × 연이율 / 1200"
                    if product_type == "saving"
                    else "원금 × 연이율 × T / 1200"
                )
            ) + ", 원 미만 절사, 세후 = 세전 × (1 - 0.154)"
        # 적용되는 인자만 남긴다: 해당 없는 기본값이 답변 LLM에 '복리 대출' 같은 설명을 지어낼 빌미를 주지 않게
        inputs = {
            "product_type": product_type,
            "amount_won": amount_won,
            "term_months": term_months,
            "rate_pct": rate_pct,
            "mode": mode,
            **(
                {"repayment": repayment}
                if loan
                else {"interest_type": "compound" if compound else "simple"}
            ),
        }
        calc = Calculation(
            name=name,
            result=result,
            after_tax=after,
            inputs=inputs,
            formula=formula,
        )
        return ToolResult(
            ok=True, tool_name="calculate", data=calc.model_dump()
        ).model_dump()
    except ValueError as e:
        return _fail("calculate", e)


@mcp.resource("product://{product_id}")
def product_doc(product_id: str) -> str:
    """상품 공시 문서(마크다운)."""
    if product_id not in CAT.by_id:
        raise ValueError(
            f"없는 상품 id: {product_id}. query_products 결과의 product_id를 쓰세요"
        )
    return to_markdown(CAT.by_id[product_id])


@mcp.prompt()
def compare_procedure(question: str) -> str:
    """비교·계산 질문의 답변 순서. 답변 규칙(수치·인용·금지 사항)은 답변 작성 시스템 프롬프트 한 곳에 둔다."""
    return f"""질문: {question}

답변 순서
1. 질문이 묻는 값(1위·금리·계산 결과)을 첫 문장에 답한다.
2. 비교 대상이 여럿이면 조회 결과 순서대로 짧게 덧붙인다.
3. 우대조건 같은 설명이 필요하면 마지막에 원문을 인용한다."""


if __name__ == "__main__":
    mcp.run(show_banner=False)
