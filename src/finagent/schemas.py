"""LLM 입출력과 도구 결과의 구조 (pydantic). 모든 출력은 성공 여부를 `ok` 키 하나로 표시한다.

- LLM 입력: *Input 모델의 to_prompt()가 만든 마크다운 텍스트를 user 메시지로 보낸다(시스템 프롬프트도 이 섹션 이름으로 지시한다).
- LLM 출력: *Output 모델의 JSON Schema로 응답 형식을 강제하고 model_validate_json으로 검증한다.
  형식이 틀리면 조용히 넘기지 않고 LLMOutputError로 실패시킨다.
- 도구 결과: ToolResult(ok, tool_name, data, error).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Route = Literal["chat", "out_of_scope", "easy", "hard"]


class LLMOutputError(RuntimeError):
    """LLM 응답이 지정한 스키마를 따르지 않음."""


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

    def to_prompt(self) -> str:
        """질문을 한 줄 프롬프트로 만든다."""
        return f"질문: {self.question}"


class RouteOutput(StrictOutput):
    # 라우팅 LLM 출력(chat·out_of_scope·easy·hard 중 하나).
    # 필드를 모두 필수로 둔다: 기본값이 있으면 제약 디코딩이 그 필드를 생략해 빈 출력이 된다(실측).
    ok: bool = Field(description="질문 유형을 판단할 수 있으면 true")
    route: Route = Field(
        description="chat=인사·잡담, out_of_scope=금융상품 공시와 무관, "
        "easy=상품 하나의 사실 조회, hard=비교·순위·계산"
    )
    reason: str = Field(description="판단 근거 한 문장")


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
        """이전 대화와 질문을 마크다운으로 만든다."""
        hist = (
            "\n".join(f"{t.role}: {t.content}" for t in self.history)
            or "(없음)"
        )
        return f"## 이전 대화\n{hist}\n\n## 사용자\n{self.question}"


class ChatOutput(StrictOutput):
    # 잡담 응답 LLM 출력.
    ok: bool = Field(description="답할 수 있으면 true")
    reply: str = Field(
        description="한국어 한두 문장. 금리 같은 수치는 쓰지 않는다"
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
