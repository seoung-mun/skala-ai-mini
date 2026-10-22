"""LLM 입출력과 도구 결과의 구조 (pydantic). 모든 출력은 성공 여부를 `ok` 키 하나로 표시한다.

- LLM 입력: *Input 모델의 to_prompt()가 만든 마크다운 텍스트를 user 메시지로 보낸다(시스템 프롬프트도 이 섹션 이름으로 지시한다).
- LLM 출력: *Output 모델의 JSON Schema로 응답 형식을 강제하고 model_validate_json으로 검증한다.
  형식이 틀리면 조용히 넘기지 않고 LLMOutputError로 실패시킨다.
- 도구 결과: ToolResult(ok, tool_name, data, error).
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Route = Literal["chat", "out_of_scope", "easy", "hard"]


class LLMOutputError(RuntimeError):
    """LLM 응답이 지정한 스키마를 따르지 않음."""

    def __init__(self, message: str, *, usage: dict | None = None):
        super().__init__(message)
        self.usage = usage


class StrictOutput(BaseModel):
    """LLM 출력 모델의 기반. 정의되지 않은 키를 스키마에서 금지한다(additionalProperties=false).
    허용하면 제약 디코딩이 막지 못해 소형 모델이 긴 키 이름을 지어내다 잘린다(qwen3:8b 실측).
    하위 모델 설명은 docstring 대신 주석으로 쓴다: docstring은 JSON Schema description이 되어 LLM에 전송된다."""

    model_config = ConfigDict(extra="forbid")


# ---------------- 도구 ----------------
class ToolResult(BaseModel):
    """MCP 도구 결과 공통 형식(ok, tool_name, data, error)."""

    ok: bool = Field(description="도구 실행 성공 여부")
    tool_name: str
    data: dict[str, Any] | None = None
    error: str | None = Field(
        default=None, description="ok=false일 때 실패 이유"
    )


# ---------------- 후속 질문 재작성 ----------------
class RewriteInput(BaseModel):
    """후속 질문 재작성 LLM 입력(직전 질문·직전 답변·이번 질문)."""

    previous_question: str
    previous_answer: str
    question: str

    def to_prompt(self) -> str:
        """LLM user 메시지로 보낼 마크다운을 만든다."""
        return (
            f"## 직전 사용자 질문\n{self.previous_question}\n\n## 직전 답변\n{self.previous_answer}\n\n"
            f"## 이번 질문\n{self.question}"
        )


class RewriteOutput(StrictOutput):
    # 후속 질문 재작성 LLM 출력.
    follow_up: bool = Field(
        description="이번 질문이 직전 대화에 기대는 후속 질문이면 true"
    )
    question: str = Field(
        description="직전 대화 없이도 이해되는 한 문장 질문. follow_up=false면 이번 "
        "질문 그대로"
    )
    reason: str = Field(description="무엇을 이어받았는지 한 문장")


# ---------------- 라우팅 ----------------
class RouteInput(BaseModel):
    """라우팅 LLM 입력."""

    question: str
    history: list[dict[str, str]] = []
    previous_conditions: dict[str, Any] | None = None
    banks: list[str] = []
    validation_error: str | None = None

    def to_prompt(self) -> str:
        return (
            f"## 이번 질문\n{self.question}\n\n"
            f"## 직전 금융 조건 (지금 요청이 이어받는 경우에만 사용)\n"
            f"{json.dumps(self.previous_conditions, ensure_ascii=False)}\n\n"
            f"## 공시에 있는 금융회사명\n{', '.join(self.banks)}\n\n"
            f"## 이전 해석의 검증 오류 (있으면 같은 원문을 다시 해석해 수정)\n{self.validation_error or '없음'}"
        )


class RouteFilters(StrictOutput):
    """사용자가 명시하거나 후속 대화에서 유지한 옵션만. 적립·이자·대출 방식이 지정되지 않았으면 해당 필드는 null이며 기본값을 가정하지 않는다"""

    rsrv_type_nm: Literal["정액적립식", "자유적립식"] | None
    rate_type_nm: Literal["단리", "복리"] | None
    mrtg_type_nm: Literal["아파트", "아파트외"] | None
    rpay_type_nm: Literal["분할상환방식", "만기일시상환방식"] | None
    lend_rate_type_nm: Literal["고정금리", "변동금리"] | None
    crdt_prdt_type: Literal["1", "2", "3"] | None


class RouteConditions(StrictOutput):
    kinds: list[Literal["deposit", "saving", "mortgage", "rent", "credit"]]
    group: Literal["bank", "savingsbank"] | None = Field(
        description="사용자가 은행만이라고 하면 bank, 저축은행만이라고 하면 savingsbank. 금융권을 말하지 않았거나 둘 다 포함이면 null"
    )
    banks: list[str] = Field(
        description="실제로 특정한 금융회사명만. 제한 없으면 []"
    )
    products: list[str] = Field(
        description="이번 조회 대상을 특정한 상품명만. 전체 후보 비교면 []. '그거'가 공동 순위 여러 상품을 가리키면 모두 넣는다. '그거랑 2위'처럼 특정 상품과 순위 후보를 함께 비교하면 특정 상품만 넣고 rank를 지정한다"
    )
    metric: Literal["rate", "rate_max", "spread", "min", "max"] | None = Field(
        description="예적금 기본금리=rate, 우대 충족 최고금리=rate_max, 우대 폭=spread, 대출 최저=min, 대출 최고=max. '금리 높은'·'이자 많이'·'제일 많이 버는'은 누구나 받는 기본금리 rate다. rate_max는 사용자가 우대 포함·최고금리를 명시했을 때만"
    )
    amount_won: int | None = Field(
        ge=1,
        description="사용자가 정한 원금·월 납입액·역산 목표를 원 단위 정수로. 단위를 생략한 저축 금액은 만원 단위다: '월 50씩'→500000, '1000 있어'→10000000, '50만원'→500000. 모르면 null",
    )
    monthly: bool = Field(description="매달 납입이면 true. 목돈 원금이면 false")
    term_months: int | None = Field(
        ge=1, description="사용자가 정한 기간을 개월로. 모르면 null"
    )
    credit_score: int | None = Field(ge=0, le=1000)
    rate_pct: float | None = Field(
        ge=0.1,
        le=30,
        description="사용자가 계산에 쓰라고 정한 연이율만. 상품에서 조회할 금리는 null",
    )
    calculation: Literal["none", "forward", "goal"] = Field(
        description="계산 없음=none, 이자·상환액=forward, 목표금액 역산=goal. '2위면 얼마나 차이나'처럼 직전에 계산한 금액으로 다른 상품의 이자를 물으면 직전 금액·기간을 넣고 forward"
    )
    repayment: Literal["equal_payment", "bullet", "equal_principal"] | None = (
        Field(
            description="대출 계산 상환방식: 원리금균등=equal_payment, 만기일시=bullet, 원금균등=equal_principal. 말하지 않았으면 null"
        )
    )
    monthly_payout: bool = Field(
        description="예적금 이자를 매달 지급받는 금액을 묻으면 true. 만기 총이자면 false"
    )
    docs: bool = Field(
        description="우대조건·가입조건·대상·한도·해지 등 상품 설명 문서가 필요하면 true"
    )
    exclude_previous: bool = Field(
        description="'다른 대안'·'그 외'·'말고 다른 거'처럼 직전에 보여준 상품을 빼고 찾으면 true"
    )
    count: int | None = Field(
        ge=1, le=50, description="사용자가 정한 보여줄 상품 개수. 없으면 null"
    )
    # OpenAI strict 스키마는 $ref 필드에 description을 허용하지 않아 설명은 RouteFilters docstring에 둔다.
    filters: RouteFilters


class RouteOutput(StrictOutput):
    # LLM이 작업과 조건을 함께 해석한다. 코드는 출력 검증과 실행만 한다.
    # 필드를 모두 필수로 둔다: 기본값이 있으면 제약 디코딩이 그 필드를 생략해 빈 출력이 된다(실측).
    ok: bool = Field(description="질문 유형을 판단할 수 있으면 true")
    operation: Literal[
        "chat",
        "recall",
        "search_documents",
        "query_rates",
        "calculate",
        "refuse",
    ] = Field(
        description="지금 수행할 일: chat=인사, recall=이전 발화 자체를 기억해 답하기, search_documents=우대조건 등 설명 문서 검색, query_rates=금리 수치·순위 조회, calculate=이자·상환액 계산, refuse=지원 범위 밖"
    )
    reason: str = Field(description="판단 근거 한 문장")
    comparison_scope: Literal["none", "market", "previous_products"] = Field(
        description="market=종류·기간 등 조건을 유지하며 전체 후보에서 상품 찾기, previous_products=명시한 후보끼리 비교, none=비교 아님"
    )
    rank: int | None = Field(
        ge=1,
        description="순위 질문이면 1부터 시작하는 순위. '가장 높은'은 1, '두번째로 높은'은 2. 아니면 null. 같은 금리는 공동 순위",
    )
    follow_up: bool = Field(
        description="이번 요청이 앞선 금융 조건을 이어받으면 true"
    )
    refusal: Literal["none", "future_rate", "unsupported"] = Field(
        description="refuse일 때 이유: 미래 금리·수익 확정=future_rate, 지원하지 않는 상품·주제=unsupported. refuse가 아니면 none"
    )
    conditions: RouteConditions
    clarify: str | None = Field(
        description="필요한 조건이 없으면 지어내지 말고 사용자에게 물을 짧은 질문. 조건이 충분하면 null"
    )

    @property
    def route(self) -> str:
        return {
            "chat": "chat",
            "recall": "recall",
            "search_documents": "easy",
            "query_rates": "hard",
            "calculate": "hard",
            "refuse": "out_of_scope",
        }[self.operation]


# ---------------- 계획 (LLM Planner) ----------------
AgentName = Literal["structured", "rag", "calc"]
KindName = Literal["deposit", "saving", "mortgage", "rent", "credit", "none"]


class PlanInput(BaseModel):
    """LLM 계획자 입력(질문과 코드가 뽑은 슬롯)."""

    question: str
    slots: dict[str, Any] = Field(
        description="코드가 질문에서 뽑은 조건(금액·기간·점수 등 숫자는 여기서만 "
        "쓴다)"
    )

    def to_prompt(self) -> str:
        """질문과 값이 있는 슬롯만 마크다운으로 만든다."""
        known = (
            ", ".join(
                f"{k}={v}"
                for k, v in self.slots.items()
                if v not in (None, [], {}, False)
            )
            or "(없음)"
        )
        return f"## 질문\n{self.question}\n\n## 코드가 뽑은 조건\n{known}"


class PlanTask(StrictOutput):
    # LLM 계획의 작업 하나(서브에이전트·상품 종류·이유).
    agent: AgentName = Field(
        description="structured=조건 조회·순위, rag=우대조건·설명 원문 검색, "
        "calc=이자·상환액 계산"
    )
    kind: KindName = Field(
        description="deposit=정기예금, saving=적금, mortgage=주담대, rent=전세대출, "
        "credit=신용대출, "
        "rag처럼 종류가 필요 없으면 none"
    )
    why: str = Field(description="이 작업이 필요한 이유 한 문장")


class PlanOutput(StrictOutput):
    # LLM 계획자 출력.
    ok: bool = Field(description="작업으로 나눌 수 있으면 true")
    tasks: list[PlanTask] = Field(
        description="필요한 작업만. 같은 agent·kind는 한 번만"
    )
    reason: str = Field(description="계획 전체의 판단 근거 한 문장")


# ---------------- 잡담 ----------------
class Turn(BaseModel):
    """대화 이력 한 턴(role, content)."""

    role: str
    content: str


class ChatInput(BaseModel):
    """잡담 응답 LLM 입력(질문·이전 대화)."""

    question: str
    history: list[Turn] = []

    def to_prompt(self) -> str:
        asked = [t.content for t in self.history if t.role == "user"][::-1]
        listed = "\n".join(
            f"{i}. {'(직전) ' if i == 1 else ''}{q}"
            for i, q in enumerate(asked, 1)
        )
        return (
            f"## 이전 사용자 질문 (이번 질문 제외, 최근 순)\n{listed or '(없음)'}\n\n"
            f"## 이번 질문\n{self.question}"
        )


class ChatOutput(StrictOutput):
    # 잡담 응답 LLM 출력.
    ok: bool = Field(description="답할 수 있으면 true")
    reply: str = Field(
        description="한국어 답변. 회상은 대화 기록을 인용할 수 있다. 기록에 없는 금융 수치는 만들지 않는다"
    )


# ---------------- 답변 작성 ----------------
class RateRow(BaseModel):
    """답변 LLM에 넘기는 금리 조회 결과 한 행."""

    product_id: str
    bank: str
    product: str
    option: str = Field(description="기간·적립방식·상환방식 등 옵션 설명")
    metric: str = Field(
        description="금리 종류(기본금리·최고금리(우대 포함)·최저금리·대출금리)"
    )
    value: float = Field(description="금리(%) 또는 우대 폭(%p)")
    unit: str = Field(default="%", description="'%' 또는 우대 폭이면 '%p'")


class Calculation(BaseModel):
    """calculate 도구의 계산 결과."""

    name: str
    result: int = Field(description="계산 결과(원)")
    after_tax: int | None = Field(
        default=None, description="세후 이자(원, 이자소득세 15.4% 반영)"
    )
    unit: str = "원"
    inputs: dict[str, Any]
    formula: str
    rate_from: str | None = Field(
        default=None, description="금리를 가져온 상품"
    )


class Description(BaseModel):
    """상품 설명 원문 한 섹션(인용 대상)."""

    section: str = Field(description="우대조건·가입 대상·중도상환수수료 등")
    text: str = Field(
        description="공시 원문. 인용은 이 text에서만 글자 그대로 한다"
    )


class ProductDoc(BaseModel):
    """답변 LLM에 넘기는 상품 문서(기본 정보·금리표·설명)."""

    product_id: str
    title: str
    info: dict[str, Any] = Field(
        description="금융회사·금융권·판매 상태·가입 방법 등"
    )
    rate_table: str = Field(
        description="금리표(마크다운). 숫자는 써도 되지만 인용(citation)하지 않는다"
    )
    rates: list[RateRow] = Field(
        default=[],
        description="상품 옵션별 검증 근거. 공시 평균금리는 제외한다",
    )
    descriptions: list[Description] = []
    data_flags: list[str] = Field(
        default=[], description="공시 데이터 이상 경고"
    )


AMOUNT_KO = {
    ("forward", "saving"): "월 납입",
    ("forward", "deposit"): "원금",
    ("forward", "loan"): "대출 원금",
    ("goal", "saving"): "목표 세후 수령액",
    ("goal", "deposit"): "목표 세후 수령액",
    ("goal", "loan"): "월 상환 가능액",
}


def _premise(i: dict) -> str:
    """계산 입력을 답변에 그대로 옮겨 쓸 수 있는 한 줄로. 해당 없는 인자는 도구가 이미 뺐다."""
    how = {
        "simple": "단리",
        "compound": "월복리",
        "equal_payment": "원리금균등",
        "bullet": "만기일시",
    }
    parts = [
        f"{AMOUNT_KO[(i['mode'], i['product_type'])]} {i['amount_won']:,}원",
        f"{i['term_months']}개월",
        f"연 {i['rate_pct']:g}%",
        how[i.get("repayment") or "equal_payment"]
        if i["product_type"] == "loan"
        else how[i.get("interest_type") or "simple"],
    ]
    return " · ".join(parts)


class RequiredFact(BaseModel):
    """원본 데이터로 확인할 답변 완료 조건. 빈 text 값은 공시 정보 없음."""

    product: str = ""
    section: str
    kind: Literal["text", "rate", "won"]
    value: str


class AnswerInput(BaseModel):
    """답변 작성 LLM 입력: 조회·계산·문서 근거와 절차·반려 사유."""

    question: str
    required_facts: list[RequiredFact] = []
    rows: list[RateRow] = Field(
        default=[], description="구조화 조회 결과. 1위부터 정렬됨"
    )
    calculations: list[Calculation] = []
    documents: list[ProductDoc] = []
    citable_ids: list[str] = Field(
        default=[], description="citations에 쓸 수 있는 product_id 목록"
    )
    procedure: str | None = Field(
        default=None, description="따라야 할 답변 절차"
    )
    previous_rejection: str | None = Field(
        default=None, description="이전 답변이 검증에서 반려된 이유"
    )
    failed: list[str] = Field(
        default=[], description="수행하지 못한 조사 작업 설명"
    )

    @property
    def compact_comparison(self) -> bool:
        # shortcut: 명시적 가입정보 비교만 축약한다. 다른 설명 질문은 전체 문서를 유지한다.
        return bool(
            self.rows
            and any(f.product and f.kind == "text" for f in self.required_facts)
            and re.search(r"가입\s*(대상|자격|방법|경로|제한)", self.question)
            and not re.search(
                r"우대|조건|수수료|유의|중도|만기\s*후|설명|특징", self.question
            )
        )

    def to_prompt(self) -> str:
        """LLM에 보낼 텍스트. 구조는 이 모델이 보장하고, 표현은 소형 모델이 읽기 쉬운 형태로 한다."""
        out = [f"## 질문\n{self.question}"]
        if self.required_facts:
            out.append(
                "## 필수 완료 조건 (모든 항목을 reply에 포함)\n"
                "상품별로 상품명을 쓰고 항목을 구분하세요. text 값은 공시 원문 그대로 "
                "써야 합니다. 값이 없으면 해당 상품·항목을 확인 불가로 표시하세요.\n"
                + "\n".join(
                    f"- {r.product} / {r.section}: "
                    + (r.value or "공시 정보 없음 — 확인 불가")
                    + (
                        next(
                            (
                                row.unit
                                for row in self.rows
                                if row.product == r.product
                                and row.metric == r.section
                            ),
                            "%",
                        )
                        if r.kind == "rate"
                        else "원"
                        if r.kind == "won"
                        else ""
                    )
                    for r in self.required_facts
                )
            )
        if self.rows:
            out.append(
                "## 조회 결과 (rows, 1위부터)\n"
                + "\n".join(
                    f"{i}. [{r.product_id}] {r.bank} {r.product} ({r.option}) {r.metric} {r.value}{r.unit}"
                    for i, r in enumerate(self.rows, 1)
                )
            )
        if self.calculations:
            out.append(
                "## 계산 결과 (calculations)\n"
                + "\n".join(
                    f"- {c.name}: "
                    + (
                        ""
                        if c.inputs.get("mode") == "goal"
                        or c.inputs.get("product_type") == "loan"
                        else "세전 "
                    )
                    + f"{c.result:,}원"
                    + (
                        f", 세후 {c.after_tax:,}원"
                        if c.after_tax is not None
                        else ""
                    )
                    + f" (전제: {_premise(c.inputs)}; 식: {c.formula}"
                    + (f"; 금리 출처: {c.rate_from}" if c.rate_from else "")
                    + ")"
                    for c in self.calculations
                )
            )
        for d in [] if self.compact_comparison else self.documents:
            desc = (
                "\n".join(f"- [{x.section}] {x.text}" for x in d.descriptions)
                or "(설명 없음)"
            )
            info = ", ".join(f"{k}: {v}" for k, v in d.info.items())
            flags = (
                ("\n데이터 주의: " + "; ".join(d.data_flags))
                if d.data_flags
                else ""
            )
            out.append(
                f"## 문서 [{d.product_id}] {d.title}\n{info}{flags}\n### 금리표 (인용 금지)\n{d.rate_table}"
                f"\n### 설명 (인용은 여기서만)\n{desc}"
            )
        if self.compact_comparison:
            out.append(
                "가입정보 출처는 서버가 원문에서 연결하므로 citations=[]로 쓰세요. "
                "reply는 상품마다 한 줄씩만 쓰고, 계산은 별도 한 문단에 쓰세요. "
                "앞뒤 요약·원문 재나열·출처 부록 없이 각 사실을 한 번만 쓰세요."
            )
        out.append(
            "## 인용 가능한 product_id\n"
            + (", ".join(self.citable_ids) or "(없음 — citations는 빈 배열)")
        )
        if self.failed:
            out.append(
                "## 수행하지 못한 조사\n"
                + "\n".join(f"- {f}" for f in self.failed)
            )
        if self.procedure:
            out.append(f"## 절차\n{self.procedure}")
        if self.previous_rejection:
            out.append(
                f"## 이전 답변이 반려된 이유 (반드시 고칠 것)\n{self.previous_rejection}"
            )
        return "\n\n".join(out)


class Citation(StrictOutput):
    # 답변의 원문 인용 하나.
    product_id: str = Field(description="citable_ids 중 하나")
    section: str = Field(description="인용한 Description의 section")
    quote: str = Field(description="Description.text의 문장을 글자 그대로 복사")


class AnswerOutput(StrictOutput):
    # 답변 작성 LLM 출력(ok, reply, citations, missing).
    ok: bool = Field(
        description="근거만으로 질문에 답할 수 있으면 true, 근거가 부족하면 false"
    )
    reply: str = Field(
        description="한국어 답변. 금리·금액은 근거의 숫자를 그대로 쓴다"
    )
    citations: list[Citation] = Field(
        description="설명 원문 인용. 없으면 빈 배열"
    )
    missing: str = Field(
        description="ok=false일 때 무엇이 부족한지, ok=true면 빈 문자열"
    )
