import operator
from typing import Annotated, TypedDict

MAX_REPLAN = 1  # 구조화 조회가 0건일 때 조건 완화 횟수
MAX_REWRITE = {"easy": 1, "hard": 2}  # 검증 실패 시 재작성 횟수
MAX_RAG_STEPS = 3  # ReAct 검색 에이전트의 LLM 판단 상한(폭주 루프 방지)


class AgentState(TypedDict, total=False):
    """부모(감독) 그래프 상태. Annotated 키는 병렬 서브에이전트 결과를 리듀서로 합친다."""

    query: str  # 후속 질문이면 재작성된 질문(이후 노드는 이것만 본다)
    original_query: str | None  # 재작성 전 사용자 원문
    thread_id: str
    history: list[dict]
    prev_slots: dict | None
    route: str
    intent: str
    slots: dict
    tasks: list[dict]  # {id, agent, args, desc, round}
    procedure: str | None
    clarify: str | None
    replan_rounds: int
    answer_input: (
        dict | None
    )  # write가 LLM에 보낸 AnswerInput (validate가 같은 근거로 검증)
    draft: dict | None
    attempts: int
    feedback: str | None
    verified: bool
    insufficient: bool
    dropped_citations: list[str]
    writing_runs: list[dict]
    final: dict
    # 서브에이전트가 병렬로 돌려준 결과·이벤트를 리듀서로 합친다(fan-in).
    results: Annotated[list[dict], operator.add]
    events: Annotated[list[dict], operator.add]
    llm_calls: Annotated[int, operator.add]
    retries: Annotated[int, operator.add]
    prompt_tokens: Annotated[int, operator.add]
    completion_tokens: Annotated[int, operator.add]
    cost_usd: Annotated[float, operator.add]


class TaskState(TypedDict, total=False):
    """서브에이전트 내부 상태. task·q·s·work는 부모에 없는 키라 부모로 새지 않는다."""

    task: dict
    q: str
    s: dict
    work: dict
    results: Annotated[list[dict], operator.add]
    events: Annotated[list[dict], operator.add]
    retries: Annotated[int, operator.add]
    # ReAct 검색 에이전트의 LLM 사용량 — 부모의 같은 키로 합쳐진다
    llm_calls: Annotated[int, operator.add]
    prompt_tokens: Annotated[int, operator.add]
    completion_tokens: Annotated[int, operator.add]
    cost_usd: Annotated[float, operator.add]
