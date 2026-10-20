import operator
from typing import Annotated, TypedDict

MAX_REPLAN = 1           # 구조화 조회가 0건일 때 조건 완화 횟수
MAX_REWRITE = {"easy": 1, "hard": 2}  # 검증 실패 시 재작성 횟수


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    prev_slots: dict | None
    t0: float
    route: str
    slots: dict
    tasks: list[dict]
    procedure: str
    clarify: str | None
    structured: list[dict]
    calcs: list[dict]
    docs: dict            # product_id -> 마크다운 원문 (근거)
    task_errors: list[str]
    replan_rounds: int
    draft: dict | None
    attempts: int
    feedback: str | None
    verified: bool
    final: dict
    # 노드가 델타만 돌려주면 누적된다(서버는 updates 스트림에서 새 이벤트만 받아 SSE로 보낸다).
    events: Annotated[list[dict], operator.add]
    llm_calls: Annotated[int, operator.add]
    retries: Annotated[int, operator.add]
    prompt_tokens: Annotated[int, operator.add]
    completion_tokens: Annotated[int, operator.add]
    cost_usd: Annotated[float, operator.add]
