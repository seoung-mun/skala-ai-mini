"""v2 그래프 조립과 실행. 노드는 functools.partial로 Deps를 주입받는다."""

from __future__ import annotations

import time
from functools import partial
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from . import nodes as N
from .agents import build_agents
from .state import AgentState


def build_graph(deps):
    """감독 노드와 서브에이전트를 연결한 v2 그래프를 컴파일한다."""
    g = StateGraph(AgentState)
    for name, fn in (
        ("route", N.route_node),
        ("chat_answer", N.chat_answer_node),
        ("refuse", N.refuse_node),
        ("plan", N.plan_node),
        ("gate", N.gate_node),
        ("replan_check", N.replan_check_node),
        ("write", N.write_node),
        ("validate", N.validate_node),
        ("finalize", N.finalize_node),
    ):
        g.add_node(name, partial(fn, deps=deps))
    agents = build_agents(deps)
    for (
        name,
        sub,
    ) in (
        agents.items()
    ):  # 컴파일된 서브그래프를 노드로 붙인다(Send로 병렬 호출)
        g.add_node(name, sub)
        g.add_edge(
            name, "replan_check"
        )  # fan-in: 같은 단계의 서브에이전트가 모두 끝난 뒤 1회 실행
    g.add_edge(START, "route")
    g.add_conditional_edges(
        "route", N.after_route, ["chat_answer", "refuse", "plan", "rag_agent"]
    )
    g.add_edge("chat_answer", END)
    g.add_edge("refuse", END)
    g.add_edge("plan", "gate")
    g.add_conditional_edges("gate", N.after_gate, [*agents, "finalize"])
    g.add_conditional_edges(
        "replan_check", N.after_replan, [*agents, "write", "finalize"]
    )
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", N.after_validate, ["write", "finalize"])
    g.add_edge("finalize", END)
    return g.compile()


def export_mermaid(app, path: Path) -> None:
    """그래프를 서브그래프 내부까지 Mermaid 텍스트로 저장한다."""
    path.write_text(
        app.get_graph(xray=True).draw_mermaid(), encoding="utf-8"
    )  # xray: 서브그래프 내부까지


def initial_state(
    query: str, thread_id: str, history: list[dict], prev_slots: dict | None
) -> dict:
    """그래프 첫 상태를 만든다(누적 키는 0·빈 목록)."""
    return {
        "query": query,
        "thread_id": thread_id,
        "history": history,
        "prev_slots": prev_slots,
        "events": [],
        "results": [],
        "llm_calls": 0,
        "retries": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
    }


def with_metadata(
    final: dict,
    state: dict,
    *,
    version: str,
    latency_ms: int,
    thread_id: str,
    data_month: str,
) -> dict:
    """FinalAnswer에 계약의 metadata(지연·LLM 호출·비용·토큰 등)를 붙인다."""
    return {
        **final,
        "metadata": {
            "version": version,
            "latency_ms": latency_ms,
            "llm_calls": state.get("llm_calls", 0),
            "retries": state.get("retries", 0),
            "validation_attempts": state.get("attempts", 0),
            "cost_usd": round(state.get("cost_usd", 0.0), 6),
            "data_month": data_month,
            "thread_id": thread_id,
            "prompt_tokens": state.get("prompt_tokens", 0),
            "completion_tokens": state.get("completion_tokens", 0),
            "writing_runs": state.get("writing_runs", []),
        },
    }


async def run_v2(
    app,
    deps,
    query: str,
    thread_id: str = "default",
    history: list[dict] | None = None,
    prev_slots: dict | None = None,
) -> tuple[dict, dict]:
    """한 번 실행해 (계약 형식 FinalAnswer, 최종 상태)를 돌려준다. 평가 스크립트가 쓴다."""
    t0 = time.perf_counter()
    state = await app.ainvoke(
        initial_state(query, thread_id, history or [], prev_slots)
    )
    final = with_metadata(
        state["final"],
        state,
        version="v2",
        latency_ms=int((time.perf_counter() - t0) * 1000),
        thread_id=thread_id,
        data_month=deps.data_month,
    )
    return final, state
