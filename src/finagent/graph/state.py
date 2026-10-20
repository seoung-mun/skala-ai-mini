import operator
from typing import Annotated, TypedDict

MAX_REPLAN = 1                        # 구조화 조회가 0건일 때 조건 완화 횟수
MAX_REWRITE = {"easy": 1, "hard": 2}  # 검증 실패 시 재작성 횟수


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    prev_slots: dict | None
    route: str
    slots: dict
    tasks: list[dict]          # {id, agent, args, desc, round}
    procedure: str | None
    clarify: str | None
    replan_rounds: int
    answer_input: dict | None  # write가 LLM에 보낸 AnswerInput (validate가 같은 근거로 검증)
    draft: dict | None
    attempts: int
    feedback: str | None
    verified: bool
    insufficient: bool
    dropped_citations: list[str]
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
