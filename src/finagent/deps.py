"""그래프 노드가 쓰는 의존성을 한곳에 모아 functools.partial로 주입한다
노드는 아래 Protocol(인터페이스)에만 의존한다. 실제 구현(OpenAI·Ollama LLM, MCP stdio 클라이언트, 하이브리드 검색기)과
테스트용 가짜 객체는 같은 메서드 모양만 맞추면 갈아 끼울 수 있다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from .catalog import Catalog
from .schemas import ToolResult

T = TypeVar("T", bound=BaseModel)


class StructuredLLM(Protocol):
    """입력 모델을 받아 출력 모델과 사용량({prompt_tokens, completion_tokens, usd})을 돌려준다."""

    def structured(
        self, system: str, payload: BaseModel, out: type[T]
    ) -> tuple[T, dict]:
        """system 프롬프트와 입력 모델로 LLM을 불러 (출력 모델, 사용량)을 돌려준다."""


class AgentLLM(StructuredLLM, Protocol):
    """도구를 스스로 부르는 에이전트용. client는 bind_tools를 지원하는 LangChain 채팅 모델(create_agent에 넘긴다),
    usage(AIMessage)는 structured()와 같은 모양의 사용량을 돌려준다."""

    client: Any

    def usage(self, message: Any) -> dict:
        """AIMessage의 토큰 사용량을 structured()와 같은 dict로 바꾼다."""


class ToolClient(Protocol):
    """MCP 도구 호출. (결과, 재시도 횟수)를 돌려주고, 실패도 ToolResult(ok=False)로 담는다."""

    async def call_tool(
        self, name: str, arguments: dict
    ) -> tuple[ToolResult, int]:
        """MCP 도구를 호출해 (ToolResult, 재시도 횟수)를 돌려준다."""

    async def get_prompt(self, name: str, arguments: dict) -> str:
        """MCP Prompt를 인자로 렌더링한 텍스트를 돌려준다."""

    async def read_resource(self, uri: str) -> str:
        """MCP Resource(예: product://{id}) 본문을 읽는다."""


class Retriever(Protocol):
    """allowed가 주어지면 그 상품 id 안에서만 찾는다. [(상품 id, 점수)]를 점수 순으로 돌려준다."""

    def search(
        self, query: str, k: int = 3, allowed: set[str] | None = None
    ) -> list[tuple[str, float]]:
        """질문과 가까운 상품을 (상품 id, 점수) 목록으로 k개까지 돌려준다."""


@dataclass
class Deps:
    """그래프 노드에 주입하는 의존성 묶음(LLM·MCP·검색기·카탈로그와 실행 방식 설정)."""

    llm: StructuredLLM
    mcp: ToolClient
    retriever: Retriever
    catalog: Catalog  # 슬롯 추출·상품 메타 (순수 조회)
    data_month: str
    # 기본값을 두지 않는다 — 기본 경로는 config(PLANNER=llm, RAG_AGENT=react) 한 곳에서만 정한다
    planner: str  # rule | llm — hard 질문의 작업 분해 방식(PLANNER)
    rag_agent: str  # fixed | react — 근거 검색 서브에이전트 방식(RAG_AGENT)
    agent_llm: AgentLLM | None = (
        None  # 계획자·검색 에이전트 모델(AGENT_MODEL). None이면 llm을 같이 쓴다
    )

    @property
    def agent(self):
        """계획자·검색 에이전트가 쓸 LLM. agent_llm이 없으면 작성 LLM(llm)을 쓴다."""
        return self.agent_llm or self.llm
