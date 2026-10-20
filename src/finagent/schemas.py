"""LLM 입출력과 도구 결과의 구조 (pydantic). 모든 출력은 성공 여부를 `ok` 키 하나로 표시한다.

- LLM 입력: *Input 모델을 JSON으로 직렬화해 user 메시지로 보낸다.
- LLM 출력: *Output 모델의 JSON Schema로 응답 형식을 강제하고 model_validate_json으로 검증한다.
  형식이 틀리면 조용히 넘기지 않고 LLMOutputError로 실패시킨다.
- 도구 결과: ToolResult(ok, tool_name, data, error).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Route = Literal["chat", "out_of_scope", "easy", "hard"]


class LLMOutputError(RuntimeError):
    """LLM 응답이 지정한 스키마를 따르지 않음."""


class StrictOutput(BaseModel):
    """LLM 출력 모델의 기반. 정의되지 않은 키를 스키마에서 금지한다(additionalProperties=false).
    허용하면 제약 디코딩이 막지 못해 소형 모델이 긴 키 이름을 지어내다 잘린다(qwen3:8b 실측)."""
    model_config = ConfigDict(extra="forbid")


# ---------------- 도구 ----------------
class ToolResult(BaseModel):
    ok: bool = Field(description="도구 실행 성공 여부")
    tool_name: str
    data: dict[str, Any] | None = None
    error: str | None = Field(default=None, description="ok=false일 때 실패 이유")


# ---------------- 라우팅 ----------------
class RouteInput(BaseModel):
    question: str

    def to_prompt(self) -> str:
        return f"질문: {self.question}"


class RouteOutput(StrictOutput):
    # 필드를 모두 필수로 둔다: 기본값이 있으면 제약 디코딩이 그 필드를 생략해 빈 출력이 된다(실측).
    ok: bool = Field(description="질문 유형을 판단할 수 있으면 true")
    route: Route = Field(description="chat=인사·잡담, out_of_scope=금융상품 공시와 무관, "
                                     "easy=상품 하나의 사실 조회, hard=비교·순위·계산")
    reason: str = Field(description="판단 근거 한 문장")


# ---------------- 잡담 ----------------
class Turn(BaseModel):
    role: str
    content: str


class ChatInput(BaseModel):
    question: str
    history: list[Turn] = []

    def to_prompt(self) -> str:
        hist = "\n".join(f"{t.role}: {t.content}" for t in self.history) or "(없음)"
        return f"## 이전 대화\n{hist}\n\n## 사용자\n{self.question}"


class ChatOutput(StrictOutput):
    ok: bool = Field(description="답할 수 있으면 true")
    reply: str = Field(description="한국어 한두 문장. 금리 같은 수치는 쓰지 않는다")


# ---------------- 답변 작성 ----------------
class RateRow(BaseModel):
    product_id: str
    bank: str
    product: str
    option: str = Field(description="기간·적립방식·상환방식 등 옵션 설명")
    metric: str = Field(description="금리 종류(기본금리·최고금리(우대 포함)·최저금리·대출금리)")
    value: float = Field(description="금리(%) 또는 우대 폭(%p)")
    unit: str = Field(default="%", description="'%' 또는 우대 폭이면 '%p'")


class Calculation(BaseModel):
    name: str
    result: int = Field(description="계산 결과(원)")
    after_tax: int | None = Field(default=None, description="세후 이자(원, 이자소득세 15.4% 반영)")
    unit: str = "원"
    inputs: dict[str, Any]
    formula: str
    rate_from: str | None = Field(default=None, description="금리를 가져온 상품")


class Description(BaseModel):
    section: str = Field(description="우대조건·가입 대상·중도상환수수료 등")
    text: str = Field(description="공시 원문. 인용은 이 text에서만 글자 그대로 한다")


class ProductDoc(BaseModel):
    product_id: str
    title: str
    info: dict[str, Any] = Field(description="금융회사·금융권·판매 상태·가입 방법 등")
    rate_table: str = Field(description="금리표(마크다운). 숫자는 써도 되지만 인용(citation)하지 않는다")
    descriptions: list[Description] = []
    data_flags: list[str] = Field(default=[], description="공시 데이터 이상 경고")


class AnswerInput(BaseModel):
    question: str
    rows: list[RateRow] = Field(default=[], description="구조화 조회 결과. 1위부터 정렬됨")
    calculations: list[Calculation] = []
    documents: list[ProductDoc] = []
    citable_ids: list[str] = Field(default=[], description="citations에 쓸 수 있는 product_id 목록")
    procedure: str | None = Field(default=None, description="따라야 할 답변 절차")
    previous_rejection: str | None = Field(default=None, description="이전 답변이 검증에서 반려된 이유")

    def to_prompt(self) -> str:
        """LLM에 보낼 텍스트. 구조는 이 모델이 보장하고, 표현은 소형 모델이 읽기 쉬운 형태로 한다."""
        out = [f"## 질문\n{self.question}"]
        if self.rows:
            out.append("## 조회 결과 (rows, 1위부터)\n" + "\n".join(
                f"{i}. [{r.product_id}] {r.bank} {r.product} ({r.option}) {r.metric} {r.value}{r.unit}"
                for i, r in enumerate(self.rows, 1)))
        if self.calculations:
            out.append("## 계산 결과 (calculations)\n" + "\n".join(
                f"- {c.name}: 세전 {c.result}원" + (f", 세후 {c.after_tax}원" if c.after_tax is not None else "")
                + f" (식: {c.formula}" + (f", 금리 출처: {c.rate_from}" if c.rate_from else "") + ")"
                for c in self.calculations))
        for d in self.documents:
            desc = "\n".join(f"- [{x.section}] {x.text}" for x in d.descriptions) or "(설명 없음)"
            info = ", ".join(f"{k}: {v}" for k, v in d.info.items())
            flags = ("\n데이터 주의: " + "; ".join(d.data_flags)) if d.data_flags else ""
            out.append(f"## 문서 [{d.product_id}] {d.title}\n{info}{flags}\n### 금리표 (인용 금지)\n{d.rate_table}"
                       f"\n### 설명 (인용은 여기서만)\n{desc}")
        out.append("## 인용 가능한 product_id\n" + (", ".join(self.citable_ids) or "(없음 — citations는 빈 배열)"))
        if self.procedure:
            out.append(f"## 절차\n{self.procedure}")
        if self.previous_rejection:
            out.append(f"## 이전 답변이 반려된 이유 (반드시 고칠 것)\n{self.previous_rejection}")
        return "\n\n".join(out)


class Citation(StrictOutput):
    product_id: str = Field(description="citable_ids 중 하나")
    section: str = Field(description="인용한 Description의 section")
    quote: str = Field(description="Description.text의 문장을 글자 그대로 복사")


class AnswerOutput(StrictOutput):
    ok: bool = Field(description="근거만으로 질문에 답할 수 있으면 true, 근거가 부족하면 false")
    reply: str = Field(description="한국어 답변. 금리·금액은 근거의 숫자를 그대로 쓴다")
    citations: list[Citation] = Field(description="설명 원문 인용. 없으면 빈 배열")
    missing: str = Field(description="ok=false일 때 무엇이 부족한지, ok=true면 빈 문자열")
