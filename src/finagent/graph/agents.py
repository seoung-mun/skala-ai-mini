"""조사 서브에이전트 3종 — 각각 컴파일된 LangGraph 서브그래프다. 부모 그래프가 Send로 필요한 것만 병렬 호출한다.

- structured_agent: 조건 조회(query_products) → 결과 점검
- rag_agent:        필터 + 하이브리드 검색 → 근거 상품 선택 (easy·hard 경로가 함께 쓴다)
- calc_agent:       금리 확보(없으면 스스로 조회) → calculate → 결과 점검
결과는 {task_id, agent, round, ok, error, ...} 한 줄로 부모의 results에 쌓인다.
"""
from __future__ import annotations

import asyncio
import time
from functools import partial

from langgraph.graph import END, START, StateGraph
from langsmith import traceable

from ..schemas import ToolResult
from ..slots import Slots
from .state import TaskState


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def allowed_ids(catalog, s: Slots) -> set[str] | None:
    if s.product_ids:
        return set(s.product_ids)
    if not (s.kinds or s.banks or s.group):
        return None
    return {p.id for p in catalog.products
            if (not s.kinds or p.kind in s.kinds) and (not s.banks or p.bank in s.banks)
            and (not s.group or p.group == s.group)}


def _done(task: dict, t0: float, ok: bool, retries: int, error: str | None, **payload) -> dict:
    return {"results": [{"task_id": task["id"], "agent": task["agent"], "round": task["round"], "ok": ok,
                         "error": error, **payload}],
            "events": [{"type": "TaskCompleted", "id": task["id"], "agent": task["agent"], "ok": ok,
                        "retries": retries, "elapsed_ms": _ms(t0)}],
            "retries": retries}


# ---------------- structured ----------------
@traceable(name="structured.query")
async def s_query(st: TaskState, deps) -> dict:
    res, r = await deps.mcp.call_tool("query_products", st["task"]["args"])
    return {"work": {"t0": time.perf_counter(), "res": res.model_dump(), "retries": r}}


@traceable(name="structured.check")
async def s_check(st: TaskState, deps) -> dict:
    w, t = st["work"], st["task"]
    res = ToolResult(**w["res"])
    rows = res.data["rows"] if res.ok else []
    return _done(t, w["t0"], res.ok, w["retries"], res.error, rows=rows)


# ---------------- rag ----------------
@traceable(name="rag.search")
async def r_search(st: TaskState, deps) -> dict:
    t0, s = time.perf_counter(), Slots(**st["s"])
    hits = await asyncio.to_thread(deps.retriever.search, st["q"], 3, allowed_ids(deps.catalog, s))
    return {"work": {"t0": t0, "hits": [i for i, _ in hits]}}


@traceable(name="rag.select")
async def r_select(st: TaskState, deps) -> dict:
    s = Slots(**st["s"])
    ids = list(dict.fromkeys(s.product_ids + st["work"]["hits"]))[:3]  # 이름이 정확히 맞은 상품을 앞에
    return _done(st["task"], st["work"]["t0"], bool(ids), 0, None if ids else "근거 문서를 찾지 못함", doc_ids=ids)


# ---------------- calc ----------------
@traceable(name="calc.resolve_rate")
async def c_resolve(st: TaskState, deps) -> dict:
    t, t0 = st["task"], time.perf_counter()
    args, retries = dict(t["args"]), 0
    if args.get("rate_pct") is not None:
        return {"work": {"t0": t0, "args": args, "retries": 0, "rate_from": None}}
    res, retries = await deps.mcp.call_tool("query_products", {**t["rate_query"], "limit": 1})
    if not res.ok or not res.data["rows"]:
        return {"work": {"t0": t0, "retries": retries, "error": res.error or "계산에 쓸 금리를 찾지 못함"}}
    top = res.data["rows"][0]
    args["rate_pct"] = top["value"]
    return {"work": {"t0": t0, "args": args, "retries": retries, "rate_from": f"{top['bank']} {top['product']} {top['option']}",
                     "rate_row": top}}


@traceable(name="calc.calculate")
async def c_calculate(st: TaskState, deps) -> dict:
    w = st["work"]
    if w.get("error"):
        return {}
    res, r = await deps.mcp.call_tool("calculate", w["args"])
    return {"work": {**w, "res": res.model_dump(), "retries": w["retries"] + r}}


@traceable(name="calc.check")
async def c_check(st: TaskState, deps) -> dict:
    w, t = st["work"], st["task"]
    if w.get("error"):
        return _done(t, w["t0"], False, w["retries"], w["error"])
    res = ToolResult(**w["res"])
    if not res.ok:
        return _done(t, w["t0"], False, w["retries"], res.error)
    calc = {**res.data, "rate_from": w["rate_from"]}
    if calc["result"] <= 0:
        return _done(t, w["t0"], False, w["retries"], f"계산 결과가 0 이하: {calc['result']}")
    return _done(t, w["t0"], True, w["retries"], None, calc=calc, rate_row=w.get("rate_row"))


def _chain(deps, *steps):
    g = StateGraph(TaskState)
    prev = START
    for name, fn in steps:
        g.add_node(name, partial(fn, deps=deps))
        g.add_edge(prev, name)
        prev = name
    g.add_edge(prev, END)
    return g.compile()


def build_agents(deps) -> dict:
    return {"structured_agent": _chain(deps, ("query", s_query), ("check", s_check)),
            "rag_agent": _chain(deps, ("search", r_search), ("select", r_select)),
            "calc_agent": _chain(deps, ("resolve_rate", c_resolve), ("calculate", c_calculate), ("check", c_check))}
