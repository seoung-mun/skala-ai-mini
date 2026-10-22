"""조사 서브에이전트 3종 — 각각 컴파일된 LangGraph 서브그래프다. 부모 그래프가 Send로 필요한 것만 병렬 호출한다.

- structured_agent: 조건 조회(query_products) → 결과 점검
- rag_agent:        필터 + 하이브리드 검색 → 근거 상품 선택 (easy·hard 경로가 함께 쓴다)
                    RAG_AGENT=react면 create_agent(네이티브 툴콜링) 에이전트가 검색·원문 읽기(MCP Resource)·선택 도구를
                    스스로 부른다(모델 호출 최대 MAX_RAG_STEPS회, 고른 상품은 본 후보 안에 있어야 한다 — 도구 안의 코드 게이트)
- calc_agent:       금리 확보(없으면 스스로 조회) → calculate → 결과 점검
결과는 {task_id, agent, round, ok, error, ...} 한 줄로 부모의 results에 쌓인다.
"""

from __future__ import annotations

import asyncio
import time
from functools import partial

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langsmith import traceable

from ..evidence import EvidenceError, EvidenceTools
from ..llm import llm_errors, session_messages
from ..schemas import LLMOutputError, ToolResult
from ..slots import Slots
from .state import MAX_RAG_STEPS, TaskState


def _ms(t0: float) -> int:
    """t0부터 지난 시간(ms)."""
    return int((time.perf_counter() - t0) * 1000)


def allowed_ids(catalog, s: Slots) -> set[str] | None:
    """슬롯(상품 id·종류·은행·금융권)으로 검색 허용 상품 id 집합을 만든다. 제한이 없으면 None."""
    if s.product_ids:
        return set(s.product_ids)
    if not (s.kinds or s.banks or s.group):
        return None
    return {
        p.id
        for p in catalog.products
        if (not s.kinds or p.kind in s.kinds)
        and (not s.banks or p.bank in s.banks)
        and (not s.group or p.group == s.group)
    }


def _done(
    task: dict, t0: float, ok: bool, retries: int, error: str | None, **payload
) -> dict:
    """서브에이전트 결과 한 줄과 TaskCompleted 이벤트를 부모 상태 델타로 만든다."""
    return {
        "results": [
            {
                "task_id": task["id"],
                "agent": task["agent"],
                "round": task["round"],
                "ok": ok,
                "error": error,
                **payload,
            }
        ],
        "events": [
            {
                "type": "TaskCompleted",
                "id": task["id"],
                "agent": task["agent"],
                "ok": ok,
                "retries": retries,
                "elapsed_ms": _ms(t0),
                "error": error,
            }
        ],  # 원문은 이벤트·추적에만
        "retries": retries,
    }


# ---------------- structured ----------------
@traceable(name="structured.query")
async def s_query(st: TaskState, deps) -> dict:
    """structured 1단계: query_products 도구를 호출한다."""
    t0 = time.perf_counter()
    res, r = await deps.mcp.call_tool("query_products", st["task"]["args"])
    return {
        "work": {
            "t0": t0,
            "res": res.model_dump(),
            "retries": r,
        }
    }


@traceable(name="structured.check")
async def s_check(st: TaskState, deps) -> dict:
    """structured 2단계: 도구 결과를 점검해 조회 행을 결과로 남긴다."""
    w, t = st["work"], st["task"]
    res = ToolResult(**w["res"])
    rows = res.data["rows"] if res.ok else []
    return _done(t, w["t0"], res.ok, w["retries"], res.error, rows=rows)


# ---------------- rag ----------------
@traceable(name="rag.search")
async def r_search(st: TaskState, deps) -> dict:
    """rag(fixed) 1단계: 허용 범위 안에서 하이브리드 검색한다. 상품명이 정확히 맞았으면 건너뛴다."""
    t0, s = time.perf_counter(), Slots(**st["s"])
    if s.product_ids:  # 허용 집합이 곧 product_ids라 검색 결과는 그 부분집합 -> r_select 결과가 같으니 모델 호출을 생략
        return {"work": {"t0": t0, "hits": []}}
    hits = await asyncio.to_thread(
        deps.retriever.search, st["q"], 3, allowed_ids(deps.catalog, s)
    )
    return {"work": {"t0": t0, "hits": [i for i, _ in hits]}}


@traceable(name="rag.select")
async def r_select(st: TaskState, deps) -> dict:
    """rag(fixed) 2단계: 이름이 맞은 상품을 앞에 두고 근거 상품을 최대 3개 고른다."""
    s = Slots(**st["s"])
    ids = list(dict.fromkeys(s.product_ids + st["work"]["hits"]))[
        :3
    ]  # 이름이 정확히 맞은 상품을 앞에
    return _done(
        st["task"],
        st["work"]["t0"],
        bool(ids),
        0,
        None if ids else "근거 문서를 찾지 못함",
        doc_ids=ids,
    )


# ---------------- rag (도구 호출 에이전트) ----------------
RAG_SYSTEM = (
    "금융상품 근거 검색 에이전트다. 질문의 근거 상품을 골라 "
    "select_evidence로 끝낸다.\n"
    "- 후보가 질문의 상품·조건과 맞는지 불확실하면 read_product로 원문을 "
    "읽는다(여러 개 동시 가능).\n"
    "- 후보가 맞지 않으면 search_products로 표현을 바꿔 검색한다(예: "
    "'월급' → '급여이체').\n"
    "- 금리 수치는 판단하지 않는다."
)


def as_langchain_tools(ev: EvidenceTools) -> list:
    """도메인 도구(EvidenceTools)를 LangChain @tool로 감싸는 얇은 어댑터. 게이트 사유는 모델이 읽도록 문자열로 돌려준다."""

    @tool
    async def search_products(query: str) -> str:
        """상품 설명을 다른 표현으로 검색. 반환: '상품id — 은행 상품명 (종류)' 줄 목록."""
        return (
            "\n".join(ev.title(i) for i in await ev.search(query))
            or "결과 없음"
        )

    @tool
    async def read_product(product_id: str) -> str:
        """후보 상품의 원문(우대조건·가입 대상·유의사항)을 읽는다. product_id: 후보 목록의 상품id."""
        try:
            return await ev.read(product_id)
        except EvidenceError as e:
            return f"거부: {e}"

    @tool(return_direct=True)
    def select_evidence(product_ids: list[str], reason: str) -> str:
        """근거 상품을 확정하고 끝낸다. product_ids: 읽거나 검색한 후보의 상품id. reason: 한 줄 이유."""
        return "선택: " + (", ".join(ev.select(product_ids, reason)) or "없음")

    return [search_products, read_product, select_evidence]


@traceable(name="rag.agent")
async def r_agent(st: TaskState, deps) -> dict:
    """첫 검색은 코드가 질문 그대로 하고, 이후 판단은 create_agent(네이티브 툴콜링)가 모델 호출 MAX_RAG_STEPS회 안에서 한다.
    상품명이 정확히 맞았으면 모델을 부르지 않는다."""
    t0, s, task = time.perf_counter(), Slots(**st["s"]), st["task"]
    if s.product_ids:
        return _done(task, t0, True, 0, None, doc_ids=s.product_ids[:3])
    ev = EvidenceTools(
        deps.retriever, deps.mcp, deps.catalog, allowed_ids(deps.catalog, s)
    )
    hits = await ev.search(st["q"])
    ev.trace.clear()  # 첫 검색은 코드가 한 것이라 에이전트 단계에 넣지 않는다
    agent = create_agent(
        deps.agent.client,
        as_langchain_tools(ev),
        system_prompt=RAG_SYSTEM,
        middleware=[
            ModelCallLimitMiddleware(
                run_limit=MAX_RAG_STEPS, exit_behavior="end"
            )
        ],
    )
    prompt = f"## 질문\n{st['q']}\n\n## 후보\n" + (
        "\n".join(ev.title(i) for i in hits) or "결과 없음"
    )
    usage, error = [], None
    try:
        with llm_errors(usage=deps.agent.usage):
            out = await agent.ainvoke(
                {"messages": [*session_messages(), HumanMessage(prompt)]}
            )
        usage = [
            deps.agent.usage(m)
            for m in out["messages"]
            if isinstance(m, AIMessage) and m.usage_metadata
        ]
    except LLMOutputError as e:  # 숨기지 않고 남긴 뒤 검색 순서로 마무리한다
        error = str(e)
        if e.usage is not None:
            usage.append(e.usage)
    if ev.picked:
        why = None
    else:  # 판단 상한·형식 오류·빈 선택 — 사실을 남기고 검색 순서로 고른다
        why = error or (
            "판단 상한 도달"
            if len(usage) >= MAX_RAG_STEPS
            else "LLM이 고른 근거 없음"
        )
    ids = (ev.picked or hits)[:3]
    delta = {
        "llm_calls": len(usage) or (1 if error else 0),
        "prompt_tokens": sum(u["prompt_tokens"] for u in usage),
        "completion_tokens": sum(u["completion_tokens"] for u in usage),
        "cost_usd": sum(u["usd"] for u in usage),
    }
    return {
        **delta,
        **_done(
            task,
            t0,
            bool(ids),
            0,
            None if ids else "근거 문서를 찾지 못함",
            doc_ids=ids,
            steps=ev.trace,
            fallback=why,
        ),
    }


# ---------------- calc ----------------
def _resolve_row(args: dict, top: dict) -> tuple[dict | None, str | None]:
    """조회한 옵션 한 줄로 계산 인자를 채운다. 계산할 수 없으면 (None, 이유)."""
    metric = top["metric"]
    loan = args["product_type"] == "loan"
    annual_rate = (
        metric in ("min", "max") or metric.startswith("대출금리@")
        if loan
        else metric in ("rate", "rate_max")
    )
    key = "repayment" if loan else "interest_type"
    source = top.get(key)
    requested = args.get(key)
    label = "상환방식" if loan else "이자 방식"
    if not annual_rate:
        return None, (
            "우대 폭·평균금리는 계산 연이율로 사용할 수 없습니다. "
            "기본금리·최고금리 또는 대출 구간 금리를 지정해 주세요."
        )
    if (source is not None and requested not in (None, source)) or (
        loan
        and top.get("rpay_type_nm") == "분할상환방식"
        and requested == "bullet"
    ):
        return None, (
            f"요청한 {label}이 공시 옵션({top['option']})과 충돌합니다. "
            "해당 방식의 상품을 지정해 주세요."
        )
    if source is None and (not loan or requested is None):
        return None, f"공시에서 계산할 {label}을 확정할 수 없습니다. " + (
            "원리금균등 또는 만기일시 중 상환방식을 알려주세요."
            if loan
            else "단리·복리 방식이 명시된 상품을 지정해 주세요."
        )
    return {**args, key: requested or source, "rate_pct": top["value"]}, None


@traceable(name="calc.resolve_rate")
async def c_resolve(st: TaskState, deps) -> dict:
    """calc 1단계: 조회한 연이율과 실제 상품의 계산 방식을 함께 검증해 채운다.
    같은 금리의 공동 순위 상품은 이자 방식이 다를 수 있어 상품마다 따로 계산한다."""
    t, t0 = st["task"], time.perf_counter()
    args, retries = dict(t["args"]), 0
    if args.get("rate_pct") is not None:
        return {
            "work": {
                "t0": t0,
                "retries": 0,
                "items": [{"args": args, "rate_from": None, "rate_row": None}],
            }
        }
    res, retries = await deps.mcp.call_tool(
        "query_products", {**t["rate_query"], "limit": 1}
    )
    if not res.ok or not res.data["rows"]:
        return {
            "work": {
                "t0": t0,
                "retries": retries,
                "error": res.error or "계산에 쓸 금리를 찾지 못함",
            }
        }
    rows = res.data["rows"]
    items, errors, skipped = [], [], []
    for top in (r for r in rows if r["value"] == rows[0]["value"]):
        resolved, error = _resolve_row(args, top)
        if error:
            errors.append(error)
            skipped.append(f"{top['bank']} {top['product']} 계산 불가: {error}")
            continue
        items.append(
            {
                "args": resolved,
                "rate_from": f"{top['bank']} {top['product']} {top['option']}",
                "rate_row": top,
            }
        )
    work = {"t0": t0, "retries": retries}
    if not items:
        return {"work": {**work, "error": errors[0], "clarify": errors[0]}}
    return {"work": {**work, "items": items, "skipped": skipped}}


@traceable(name="calc.calculate")
async def c_calculate(st: TaskState, deps) -> dict:
    """calc 2단계: calculate 도구를 상품마다 호출한다(앞 단계가 실패했으면 건너뛴다)."""
    w = st["work"]
    if w.get("error"):
        return {}
    items, retries = [], w["retries"]
    for item in w["items"]:
        res, r = await deps.mcp.call_tool("calculate", item["args"])
        items.append({**item, "res": res.model_dump()})
        retries += r
    return {"work": {**w, "items": items, "retries": retries}}


@traceable(name="calc.check")
async def c_check(st: TaskState, deps) -> dict:
    """calc 3단계: 계산 결과를 점검해 결과로 남긴다(도구 실패·0 이하는 ok=False)."""
    w, t = st["work"], st["task"]
    if w.get("error"):
        return _done(
            t,
            w["t0"],
            False,
            w["retries"],
            w["error"],
            clarify=w.get("clarify"),
        )
    calcs, rate_rows = [], []
    for item in w["items"]:
        res = ToolResult(**item["res"])
        if not res.ok:
            return _done(t, w["t0"], False, w["retries"], res.error)
        calc = {**res.data, "rate_from": item["rate_from"]}
        if calc["result"] <= 0:
            return _done(
                t,
                w["t0"],
                False,
                w["retries"],
                f"계산 결과가 0 이하: {calc['result']}",
            )
        calcs.append(calc)
        if item["rate_row"]:
            rate_rows.append(item["rate_row"])
    return _done(
        t,
        w["t0"],
        True,
        w["retries"],
        None,
        calcs=calcs,
        rate_rows=rate_rows,
        skipped=w.get("skipped", []),
    )


def _chain(deps, *steps):
    """단계 함수들을 순서대로 잇는 서브그래프를 컴파일한다."""
    g = StateGraph(TaskState)
    prev = START
    for name, fn in steps:
        g.add_node(name, partial(fn, deps=deps))
        g.add_edge(prev, name)
        prev = name
    g.add_edge(prev, END)
    return g.compile()


def build_agents(deps) -> dict:
    """서브에이전트 3종(structured·rag·calc)을 만든다. rag는 RAG_AGENT 설정(react|fixed)을 따른다."""
    return {
        "structured_agent": _chain(
            deps, ("query", s_query), ("check", s_check)
        ),
        "rag_agent": _chain(deps, ("agent", r_agent))
        if deps.rag_agent == "react"
        else _chain(deps, ("search", r_search), ("select", r_select)),
        "calc_agent": _chain(
            deps,
            ("resolve_rate", c_resolve),
            ("calculate", c_calculate),
            ("check", c_check),
        ),
    }
