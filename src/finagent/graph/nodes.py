"""v2 그래프 노드. 모두 (state, deps) -> 상태 델타를 돌려주는 async 함수다.

숫자(금리·순위·이자)는 MCP 도구만 만들고, LLM은 근거를 읽고 답·인용을 JSON으로 쓰며, 검증은 코드가 한다.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from langsmith import traceable

from ..catalog import LOAN_KINDS
from ..mcp_client import ToolFailed
from ..parser import KIND_KO, to_markdown
from ..slots import Slots, extract, route_rules
from ..validate import check_answer
from .state import MAX_REPLAN, MAX_REWRITE

TEXT_CUE = r"우대|조건|가입|대상|수수료|연체|한도|해지|유의|방법|언제|누가"
WINNER_RE = r"제일|가장|더\s*(높|낮|좋|나)|어디|어느|뭐가|1위"
METRIC_KO = {"rate": "기본금리", "rate_max": "최고금리(우대 포함)", "min": "최저금리", "max": "최고금리"}
ROUTE_SCHEMA = {"type": "object", "required": ["route"],
                "properties": {"route": {"type": "string", "enum": ["chat", "out_of_scope", "easy", "hard"]}}}
ANSWER_SCHEMA = {
    "type": "object", "required": ["reply", "citations"],
    "properties": {"reply": {"type": "string"},
                   "citations": {"type": "array", "items": {
                       "type": "object", "required": ["product_id", "section", "quote"],
                       "properties": {"product_id": {"type": "string"}, "section": {"type": "string"},
                                      "quote": {"type": "string"}}}}}}
ROUTE_SYSTEM = ("금융상품 질문 분류기다. route를 하나 고른다. chat=인사·잡담, out_of_scope=예금·적금·주담대·"
                "전세대출·신용대출 공시와 무관, easy=상품 하나의 사실 조회, hard=여러 상품 비교·순위·이자/상환액 계산.")
CHAT_SYSTEM = ("금융상품 공시 안내 챗봇이다. 인사·잡담에 한국어 한두 문장으로 답하고, 정기예금·적금·주택담보대출·"
               "전세자금대출·신용대출의 금감원 공시 정보를 비교·계산해 줄 수 있다고 안내하라. 금리 같은 수치는 말하지 마라.")
WRITE_SYSTEM = ("금융상품 상담원이다. '근거'에 있는 내용만으로 한국어로 간결하게 답하라.\n"
                "- 금리·금액은 근거의 숫자를 그대로 쓰고, 직접 계산하거나 반올림하지 마라. 계산 결과가 있으면 반드시 포함하라.\n"
                "- 1위가 여러 개면 모두 말하라. 공시 평균금리는 말하지 마라.\n"
                "- citations에는 '상품 설명' 아래 문장만 글자 그대로 복사해 넣는다(표는 인용 금지, 인용할 문장이 없으면 빈 배열). "
                "product_id는 문서 제목의 [ ] 안 값을 쓴다.\n"
                "- 근거에 없는 내용은 모른다고 답하라.")
REFUSE_REPLY = ("죄송해요, 저는 금감원에 공시된 정기예금·적금·주택담보대출·전세자금대출·신용대출 정보만 안내할 수 있어요. "
                "이 범위의 상품을 물어봐 주세요.")
WARNING = "{y}년 {m}월 공시 기준이며 실제 적용 금리는 개인 조건에 따라 다릅니다. 가입·대출 권유가 아닙니다."


async def _llm(deps, system: str, user: str, schema: dict | None = None) -> tuple[str, dict]:
    text, u = await asyncio.to_thread(deps.llm.chat, system, user, schema)
    return text, {"llm_calls": 1, "prompt_tokens": u.get("prompt_tokens", 0),
                  "completion_tokens": u.get("completion_tokens", 0), "cost_usd": u.get("usd", 0.0)}


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _allowed(deps, s: Slots) -> set[str] | None:
    if s.product_ids:
        return set(s.product_ids)
    if not (s.kinds or s.banks or s.group):
        return None
    return {p.id for p in deps.catalog.products
            if (not s.kinds or p.kind in s.kinds) and (not s.banks or p.bank in s.banks)
            and (not s.group or p.group == s.group)}


# ---------------- route ----------------
@traceable(name="route_node")
async def route_node(state, deps) -> dict:
    q = state["query"]
    s = extract(q, deps.catalog)
    prev = state.get("prev_slots")
    if prev and not (s.kinds or s.product_ids):  # 후속 질문("그거 24개월은?")은 직전 대상을 이어받는다
        s.kinds, s.product_ids, s.banks = prev["kinds"], prev["product_ids"], prev["banks"]
        s.group = s.group or prev.get("group")
    route, reason = route_rules(q, s)
    delta: dict = {}
    if route is None:
        text, delta = await _llm(deps, ROUTE_SYSTEM, q, ROUTE_SCHEMA)
        route = json.loads(text).get("route")
        if route not in ("chat", "out_of_scope", "easy", "hard"):
            raise ValueError(f"LLM 라우팅 결과가 올바르지 않습니다: {text[:80]}")
        reason = "규칙으로 판단 불가 → LLM 분류"
    return {**delta, "route": route, "slots": s.to_dict(),
            "events": [{"type": "RouteDecided", "route": route, "reason": reason}]}


@traceable(name="chat_answer_node")
async def chat_answer_node(state, deps) -> dict:
    hist = "\n".join(f"{m['role']}: {m['content']}" for m in state.get("history", [])[-6:])
    reply, u = await _llm(deps, CHAT_SYSTEM, f"## 이전 대화\n{hist or '(없음)'}\n\n## 사용자\n{state['query']}")
    return {**u, "final": _final(state, deps, reply.strip())}


@traceable(name="refuse_node")
async def refuse_node(state, deps) -> dict:
    return {"final": _final(state, deps, REFUSE_REPLY)}


# ---------------- easy ----------------
@traceable(name="retrieve_node")
async def retrieve_node(state, deps) -> dict:
    s, t0 = Slots(**state["slots"]), time.perf_counter()
    hits = await asyncio.to_thread(deps.retriever.search, state["query"], 3, _allowed(deps, s))
    ids = list(dict.fromkeys(s.product_ids + [i for i, _ in hits]))[:3]  # 이름이 정확히 맞은 상품을 앞에
    return {"docs": {i: to_markdown(deps.catalog.by_id[i]) for i in ids},
            "events": [{"type": "TaskCompleted", "id": "t1", "agent": "rag", "ok": bool(ids), "retries": 0,
                        "elapsed_ms": _ms(t0)}]}


# ---------------- hard ----------------
@traceable(name="plan_node")
async def plan_node(state, deps) -> dict:
    q, s = state["query"], Slots(**state["slots"])
    tasks: list[dict] = []
    m = re.search(r"(\d+)\s*개", q)
    limit = int(m.group(1)) if m else 5
    for kind in s.kinds:
        if not (s.compare or s.product_ids or (s.calc and s.rate_pct is None)):
            continue
        args = {"kind": kind, "group": s.group, "term_months": s.term_months, "metric": s.metric,
                "credit_score": s.credit_score, "filters": s.filters or None, "limit": limit}
        ids = [i for i in s.product_ids if deps.catalog.by_id[i].kind == kind]
        if ids:
            args["product_ids"] = ids
        elif s.banks:
            args["banks"] = s.banks
        args = {k: v for k, v in args.items() if v is not None}
        tasks.append({"agent": "structured", "args": args,
                      "desc": f"{KIND_KO[kind]} {METRIC_KO.get(args.get('metric') or '', '금리')} 조회"})
    if s.amount_won:
        loan = any(k in LOAN_KINDS for k in s.kinds) or re.search(r"빌리|빌려|대출|갚|상환", q)
        if loan:
            tool, args = "calc_loan_payment", {"principal_won": s.amount_won,
                                               "method": "bullet" if "만기일시" in q else "equal_payment"}
        elif "saving" in s.kinds or s.monthly:
            tool, args = "calc_saving_interest", {"monthly_won": s.amount_won, "compound": "복리" in q}
        else:
            tool, args = "calc_deposit_interest", {"principal_won": s.amount_won, "compound": "복리" in q}
        args.update({"term_months": s.term_months, "rate_pct": s.rate_pct})
        tasks.append({"agent": "calc", "tool": tool, "args": args, "desc": f"{tool} 계산"})
    if re.search(TEXT_CUE, q) or (s.product_ids and not s.compare):
        tasks.append({"agent": "rag", "args": {}, "desc": "우대조건·설명 원문 검색"})
    for i, t in enumerate(tasks, 1):
        t.update(id=f"t{i}", status="pending")
    procedure = await deps.mcp.get_prompt("compare_procedure", {"question": q})
    return {"tasks": tasks, "procedure": procedure, "replan_rounds": 0,
            "events": [{"type": "PlanCreated", "tasks": [{k: t[k] for k in ("id", "agent", "desc")} for t in tasks]}]}


@traceable(name="gate_node")
async def gate_node(state, deps) -> dict:
    """계획을 코드로 검사한다(베이스 권위 게이트). 실행 불가능한 task는 빼고, 필요한 정보는 되묻는다."""
    kept, clarify = [], None
    for t in state["tasks"]:
        a = t["args"]
        if t["agent"] == "structured" and a["kind"] == "credit" and a.get("credit_score") is None:
            clarify = "신용대출 금리는 신용점수 구간마다 달라요. 신용점수(예: 750점)를 알려주시면 비교해 드릴게요."
            continue
        if t["agent"] == "calc" and a.get("term_months") is None:
            clarify = "기간(예: 12개월, 30년)을 알려주시면 계산해 드릴게요."
            continue
        kept.append(t)
    if any(t["agent"] == "calc" and t["args"].get("rate_pct") is None for t in kept) and \
            not any(t["agent"] == "structured" for t in kept):
        kept = [t for t in kept if t["agent"] != "calc"]
        clarify = "금리를 알려주시거나 상품을 지정해 주시면 계산해 드릴게요."
    if not kept and not clarify:
        clarify = "어떤 상품(정기예금·적금·주담대·전세대출·신용대출)을 비교할지 알려주세요."
    return {"tasks": kept, "clarify": clarify}


@traceable(name="research_node")
async def research_node(state, deps) -> dict:
    """구조화 조회와 원문 검색을 병렬로 실행하고, 계산은 조회한 금리가 필요할 수 있어 그 뒤에 실행한다."""
    q, s = state["query"], Slots(**state["slots"])
    tasks = [dict(t) for t in state["tasks"]]
    structured, calcs = list(state.get("structured", [])), list(state.get("calcs", []))
    docs, errors, events, retries = dict(state.get("docs", {})), [], [], 0

    async def run(t):
        t0 = time.perf_counter()
        try:
            if t["agent"] == "structured":
                res, r = await deps.mcp.call_tool("query_products", t["args"])
                return t, res["rows"], r, None, _ms(t0)
            if t["agent"] == "rag":
                hits = await asyncio.to_thread(deps.retriever.search, q, 3, _allowed(deps, s))
                return t, [i for i, _ in hits], 0, None, _ms(t0)
            args = dict(t["args"])
            if args.get("rate_pct") is None:
                if not structured:
                    raise ValueError("계산에 쓸 금리를 조회하지 못했습니다")
                top = structured[0]
                args["rate_pct"], t["rate_from"] = top["value"], f"{top['bank']} {top['product']} {top['option']}"
            res, r = await deps.mcp.call_tool(t["tool"], args)
            return t, res, r, None, _ms(t0)
        except (ToolFailed, ValueError, asyncio.TimeoutError) as e:
            return t, None, 0, f"{t['desc']}: {e}", _ms(t0)

    def record(results):
        nonlocal retries, structured
        for t, out, r, err, ms in results:
            t["status"], retries = "done", retries + r
            events.append({"type": "TaskCompleted", "id": t["id"], "agent": t["agent"], "ok": err is None,
                           "retries": r, "elapsed_ms": ms})
            if err:
                t["failed"] = True
                errors.append(err)
            elif t["agent"] == "structured":
                t["rows"] = len(out)
                structured = [x for x in structured if x.get("task") != t["id"]] + [dict(x, task=t["id"]) for x in out]
                docs.update({x["product_id"]: to_markdown(deps.catalog.by_id[x["product_id"]]) for x in out[:3]})
            elif t["agent"] == "rag":
                docs.update({i: to_markdown(deps.catalog.by_id[i]) for i in out})
            else:
                calcs.append({**out, "from": t.get("rate_from")})

    pending = [t for t in tasks if t["status"] == "pending"]
    record(await asyncio.gather(*(run(t) for t in pending if t["agent"] != "calc")))
    record(await asyncio.gather(*(run(t) for t in pending if t["agent"] == "calc")))
    return {"tasks": tasks, "structured": structured, "calcs": calcs, "docs": docs, "retries": retries,
            "task_errors": state.get("task_errors", []) + errors, "events": events}


@traceable(name="replan_check_node")
async def replan_check_node(state, deps) -> dict:
    """구조화 조회가 0건이면 조건을 한 단계 완화해 다시 조사한다(최대 MAX_REPLAN회)."""
    rounds, tasks = state.get("replan_rounds", 0), [dict(t) for t in state["tasks"]]
    empty = [t for t in tasks if t["agent"] == "structured" and t.get("rows") == 0]
    if not empty or rounds >= MAX_REPLAN:
        why = "조회 결과 있음" if not empty else f"완화 상한({MAX_REPLAN}회) 도달"
        return {"events": [{"type": "ReplanCheck", "round": rounds, "is_sufficient": True, "reason": why}]}
    dropped = []
    for t in empty:
        key = next((k for k in ("filters", "term_months", "group") if k in t["args"]), None)
        if key:
            t["args"] = {k: v for k, v in t["args"].items() if k != key}
            t["status"] = "pending"
            dropped.append(key)
    if not dropped:
        return {"events": [{"type": "ReplanCheck", "round": rounds, "is_sufficient": True,
                            "reason": "더 완화할 조건 없음"}]}
    for t in tasks:  # 금리를 못 얻어 실패한 계산도 다시 돌린다
        if t["agent"] == "calc" and t.get("failed"):
            t["status"], t["failed"] = "pending", False
    return {"tasks": tasks, "replan_rounds": rounds + 1, "task_errors": [],
            "events": [{"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
                        "reason": f"조회 0건 → 조건 완화: {', '.join(dropped)} 제거"}]}


# ---------------- write / validate / finalize ----------------
def evidence_text(state) -> str:
    lines = []
    for r in state.get("structured", []):
        lines.append(f"- 조회: [{r['product_id']}] {r['bank']} {r['product']} ({r['option']}) "
                     f"{METRIC_KO.get(r['metric'], '대출금리')} {r['value']}%")
    for c in state.get("calcs", []):
        lines.append(f"- 계산: {c['name']} = {c['result']}원 (입력 {json.dumps(c['inputs'], ensure_ascii=False)}, "
                     f"식 {c['formula']}"
                     + (f", 금리 출처 {c['from']}" if c.get("from") else "") + ")")
    for pid, md in state.get("docs", {}).items():
        body = md.split("---", 2)[-1]
        lines.append(f"\n### 문서 [{pid}]\n{body.strip()}")
    return "\n".join(lines) or "(근거 없음)"


@traceable(name="write_node")
async def write_node(state, deps) -> dict:
    system = WRITE_SYSTEM + (f"\n\n{state['procedure']}" if state.get("procedure") else "")
    ids = list(state.get("docs", {}))
    cite_rule = (f"citations의 product_id는 다음 중 하나만: {', '.join(ids)}" if ids
                 else "인용할 문서가 없으므로 citations는 반드시 빈 배열 []")
    user = f"## 질문\n{state['query']}\n\n## 근거\n{evidence_text(state)}\n\n## 인용 규칙\n{cite_rule}"
    if state.get("feedback"):
        user += f"\n\n## 이전 답변이 검증에서 반려된 이유 (반드시 고칠 것)\n{state['feedback']}"
    text, u = await _llm(deps, system, user, ANSWER_SCHEMA)
    try:
        draft = json.loads(text)
        if not isinstance(draft.get("reply"), str) or not isinstance(draft.get("citations"), list):
            raise ValueError
    except ValueError:  # JSONDecodeError 포함 — 숨기지 않고 검증 실패로 기록한다
        draft = {"reply": "", "citations": [],
                 "_error": f"LLM 출력이 JSON 형식이 아님(길이 {len(text)}, 끝: {text[-40:]!r})"}
    return {**u, "draft": draft}


@traceable(name="validate_node")
async def validate_node(state, deps) -> dict:
    d, attempts = state["draft"], state.get("attempts", 0) + 1
    if d.get("_error"):
        ok, why = False, d["_error"]
    elif not d["reply"].strip():
        ok, why = False, "빈 답변"
    else:
        ok, why = check_answer(d["reply"], evidence_text(state) + "\n" + state["query"], d["citations"],
                               state.get("docs", {}), required=[c["result"] for c in state.get("calcs", [])],
                               winner=_winner(state))
    return {"attempts": attempts, "verified": ok, "feedback": why,
            "events": [{"type": "Validation", "attempt": attempts, "verdict": "pass" if ok else "fail",
                        "reason": why}]}


def _winner(state) -> tuple[list[str], list[str]] | None:
    rows = state.get("structured", [])
    if not rows or not re.search(WINNER_RE, state["query"]):
        return None
    names = lambda rs: [n for r in rs for n in (r["product"], r["bank"].replace("주식회사", "").strip())]
    return names([r for r in rows if r["value"] == rows[0]["value"]]), names(rows)


def _card(deps, pid: str, highlight: dict | None = None) -> dict:
    p = deps.catalog.by_id[pid]
    end = p.base["dcls_end_day"]
    return {"id": p.id, "kind": p.kind, "kind_ko": KIND_KO[p.kind], "group": p.group, "bank": p.bank,
            "product": p.name, "sale": "판매중" if not end else f"공시 종료일 {end}", "highlight": highlight,
            "data_flags": p.flags}


def _final(state, deps, reply, products=(), citations=(), calcs=(), extra=()) -> dict:
    y, m = deps.data_month[:4], deps.data_month[4:]
    return {"reply": reply, "route": state["route"], "products": list(products), "citations": list(citations),
            "calculations": [{k: c[k] for k in ("name", "inputs", "formula", "result", "unit")} for c in calcs],
            "warnings": [WARNING.format(y=y, m=int(m))] + list(extra)}


def _table_reply(state) -> str:
    out = []
    rows = state.get("structured", [])
    if rows:
        out.append("공시 데이터 조회 결과예요.")
        out += [f"{i}. {r['bank']} {r['product']} — {r['option']} {METRIC_KO.get(r['metric'], '대출금리')} {r['value']}%"
                for i, r in enumerate(rows[:5], 1)]
    for c in state.get("calcs", []):
        out.append(f"{c['name']}: {c['result']:,}원 ({c['formula']})")
    return "\n".join(out) or "조건에 맞는 상품을 찾지 못했어요. 조건을 바꿔 다시 물어봐 주세요."


@traceable(name="finalize_node")
async def finalize_node(state, deps) -> dict:
    route, extra = state["route"], list(state.get("task_errors", []))
    if state.get("clarify") and not state.get("tasks"):
        return {"final": _final(state, deps, state["clarify"])}
    if state.get("clarify"):
        extra.append(state["clarify"])
    rows, calcs = state.get("structured", []), state.get("calcs", [])
    if state.get("verified"):
        reply, cites = state["draft"]["reply"], state["draft"]["citations"]
    else:
        cites = []
        if route == "hard":
            reply = _table_reply(state)
            extra.append("답변이 검증을 통과하지 못해 도구 결과를 그대로 보여드려요.")
        else:
            names = [deps.catalog.by_id[i].name for i in state.get("docs", {})]
            reply = "검증된 답을 만들지 못했어요. 아래 상품의 공시 내용을 직접 확인해 주세요: " + ", ".join(names)
    if route == "hard" and rows:
        products = []
        for r in rows:
            if r["product_id"] not in {p["id"] for p in products} and len(products) < 3:
                products.append(_card(deps, r["product_id"], {
                    "label": f"{r['option']} {METRIC_KO.get(r['metric'], '대출금리')}", "value": r["value"],
                    "unit": "%"}))
    else:
        ids = [c["product_id"] for c in cites if c.get("product_id") in deps.catalog.by_id] \
            or list(state.get("docs", {}))[:1]
        products = [_card(deps, i) for i in dict.fromkeys(ids)]
    return {"final": _final(state, deps, reply, products, cites, calcs, extra)}


# ---------------- 분기 ----------------
def after_route(state) -> str:
    return {"chat": "chat_answer", "out_of_scope": "refuse", "easy": "retrieve", "hard": "plan"}[state["route"]]


def after_gate(state) -> str:
    return "research" if state["tasks"] else "finalize"


def after_replan(state) -> str:
    return "research" if any(t["status"] == "pending" for t in state["tasks"]) else "write"


def after_validate(state) -> str:
    if state["verified"] or state["attempts"] > MAX_REWRITE[state["route"]]:
        return "finalize"
    return "write"
