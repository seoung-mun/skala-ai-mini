"""v2 감독(supervisor) 노드. 모두 (state, deps) -> 상태 델타를 돌려주는 async 함수다.

숫자(금리·순위·이자)는 서브에이전트가 MCP 도구로만 만들고, LLM은 구조화된 근거(AnswerInput)를 읽고
AnswerOutput(ok, reply, citations)을 쓰며, 검증은 코드가 한다.
"""
from __future__ import annotations

import asyncio
import re

from langgraph.types import Send
from langsmith import traceable

from ..catalog import LOAN_KINDS, core_name
from ..parser import GROUP_KO, KIND_KO, descriptions, rate_table
from ..schemas import (AnswerInput, AnswerOutput, Calculation, ChatInput, ChatOutput, Description,
                       LLMOutputError, ProductDoc, RateRow, RouteInput, RouteOutput, Turn)
from ..slots import Slots, extract, route_rules
from ..validate import allowed_values, check_answer, split_citations
from .state import MAX_REPLAN, MAX_REWRITE

TEXT_CUE = r"우대|조건|가입|대상|수수료|연체|한도|해지|유의|방법|언제|누가"
WINNER_RE = r"제일|가장|더\s*(높|낮|좋|나)|어디|어느|뭐가|1위"
METRIC_KO = {"rate": "기본금리", "rate_max": "최고금리(우대 포함)", "min": "최저금리", "max": "최고금리"}
ROUTE_SYSTEM = "금융상품 질문 분류기다. 입력 JSON의 question을 읽고 지정된 JSON 형식으로만 답한다."
CHAT_SYSTEM = ("금융상품 공시 안내 챗봇이다. 인사·잡담에 한국어 한두 문장으로 답하고, 정기예금·적금·주택담보대출·"
               "전세자금대출·신용대출의 금감원 공시 정보를 비교·계산해 줄 수 있다고 안내한다. 지정된 JSON 형식으로만 답한다.")
WRITE_SYSTEM = ("금융상품 상담원이다. 입력 JSON의 근거(rows, calculations, documents)만으로 question에 답한다.\n"
                "- 금리·금액은 근거의 숫자를 그대로 쓰고 직접 계산하거나 반올림하지 않는다. calculations가 있으면 결과를 반드시 쓴다.\n"
                "- rows는 1위부터 정렬돼 있다. 1위를 묻는 질문이면 1위부터 답하고, 동률이면 모두 말한다.\n"
                "- citations는 documents.descriptions의 text에서만 글자 그대로 복사한다. rate_table은 인용하지 않는다. "
                "product_id는 citable_ids 중에서 고르고, 인용할 문장이 없으면 빈 배열로 둔다.\n"
                "- 근거가 질문에 답하기에 부족하면 ok=false와 missing을 쓴다. 공시 평균금리는 말하지 않는다.\n"
                "- reply에는 product_id 같은 내부 식별자를 쓰지 말고 은행명과 상품명으로 말한다.\n"
                "- previous_rejection이 있으면 그 이유를 반드시 고친다. 지정된 JSON 형식으로만 답한다.")
REFUSE_REPLY = ("죄송해요, 저는 금감원에 공시된 정기예금·적금·주택담보대출·전세자금대출·신용대출 정보만 안내할 수 있어요. "
                "이 범위의 상품을 물어봐 주세요.")
WARNING = "{y}년 {m}월 공시 기준이며 실제 적용 금리는 개인 조건에 따라 다릅니다. 가입·대출 권유가 아닙니다."


async def _llm(deps, system: str, payload, out):
    """구조화 LLM 호출. (출력 모델, 사용량 델타)를 돌려준다. 형식 오류는 LLMOutputError로 올린다."""
    result, u = await asyncio.to_thread(deps.llm.structured, system, payload, out)
    return result, {"llm_calls": 1, "prompt_tokens": u.get("prompt_tokens", 0),
                    "completion_tokens": u.get("completion_tokens", 0), "cost_usd": u.get("usd", 0.0)}


def _task(i: int, agent: str, desc: str, args: dict | None = None, **extra) -> dict:
    return {"id": f"t{i}", "agent": agent, "desc": desc, "args": args or {}, "round": 0, **extra}


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
        out, delta = await _llm(deps, ROUTE_SYSTEM, RouteInput(question=q), RouteOutput)
        route, reason = (out.route, f"LLM 분류: {out.reason}") if out.ok else \
            ("chat", "분류 불가 → 대화로 무엇이 필요한지 묻기")
    tasks = [_task(1, "rag", "우대조건·설명 원문 검색")] if route == "easy" else []
    return {**delta, "route": route, "slots": s.to_dict(), "tasks": tasks,
            "events": [{"type": "RouteDecided", "route": route, "reason": reason}]}


@traceable(name="chat_answer_node")
async def chat_answer_node(state, deps) -> dict:
    hist = [Turn(**m) for m in state.get("history", [])[-6:]]
    out, u = await _llm(deps, CHAT_SYSTEM, ChatInput(question=state["query"], history=hist), ChatOutput)
    reply = out.reply.strip() if out.ok and out.reply.strip() else "무엇을 도와드릴까요? 예금·적금·대출 상품을 물어봐 주세요."
    return {**u, "final": _final(state, deps, reply)}


@traceable(name="refuse_node")
async def refuse_node(state, deps) -> dict:
    return {"final": _final(state, deps, REFUSE_REPLY)}


# ---------------- hard: plan / gate ----------------
@traceable(name="plan_node")
async def plan_node(state, deps) -> dict:
    q, s = state["query"], Slots(**state["slots"])
    tasks: list[dict] = []
    m = re.search(r"(\d+)\s*개", q)
    limit = int(m.group(1)) if m else 5

    def query_args(kind: str) -> dict:
        args = {"kind": kind, "group": s.group, "term_months": s.term_months,
                "metric": None if kind == "credit" else s.metric,
                "credit_score": s.credit_score, "filters": s.filters or None, "limit": limit}
        ids = [i for i in s.product_ids if deps.catalog.by_id[i].kind == kind]
        if ids:
            args["product_ids"] = ids
        elif s.banks:
            args["banks"] = s.banks
        return {k: v for k, v in args.items() if v is not None}

    for kind in s.kinds:
        if s.compare or (s.product_ids and not s.amount_won):
            a = query_args(kind)
            tasks.append(_task(len(tasks) + 1, "structured",
                               f"{KIND_KO[kind]} {METRIC_KO.get(a.get('metric', ''), '금리')} 조회", a))
    if s.amount_won:
        loan = any(k in LOAN_KINDS for k in s.kinds) or bool(re.search(r"빌리|빌려|대출|갚|상환", q))
        ptype = "loan" if loan else ("saving" if ("saving" in s.kinds or s.monthly) else "deposit")
        args = {"product_type": ptype, "amount_won": s.amount_won, "term_months": s.term_months,
                "rate_pct": s.rate_pct, "interest_type": "compound" if "복리" in q else "simple",
                "repayment": "bullet" if "만기일시" in q else "equal_payment"}
        kind = next((k for k in s.kinds if (k in LOAN_KINDS) == loan), None)
        tasks.append(_task(len(tasks) + 1, "calc", f"{ptype} 계산", args,
                           rate_query=query_args(kind) if kind else None))
    if re.search(TEXT_CUE, q) or (s.product_ids and not s.compare):
        tasks.append(_task(len(tasks) + 1, "rag", "우대조건·설명 원문 검색"))
    procedure = await deps.mcp.get_prompt("compare_procedure", {"question": q})
    return {"tasks": tasks, "procedure": procedure, "replan_rounds": 0,
            "events": [{"type": "PlanCreated", "tasks": [{k: t[k] for k in ("id", "agent", "desc")} for t in tasks]}]}


@traceable(name="gate_node")
async def gate_node(state, deps) -> dict:
    """계획을 코드로 검사한다(베이스 권위 게이트). 실행 불가능한 task는 빼고, 필요한 정보는 되묻는다."""
    kept, clarify = [], None
    for t in state["tasks"]:
        a, rq = t["args"], t.get("rate_query") or {}
        if t["agent"] == "structured" and a["kind"] == "credit" and a.get("credit_score") is None:
            clarify = "신용대출 금리는 신용점수 구간마다 달라요. 신용점수(예: 750점)를 알려주시면 비교해 드릴게요."
        elif t["agent"] == "calc" and a.get("term_months") is None:
            clarify = "기간(예: 12개월, 30년)을 알려주시면 계산해 드릴게요."
        elif t["agent"] == "calc" and a.get("rate_pct") is None and not rq:
            clarify = "금리를 알려주시거나 상품을 지정해 주시면 계산해 드릴게요."
        elif t["agent"] == "calc" and a.get("rate_pct") is None and rq.get("kind") == "credit" \
                and rq.get("credit_score") is None:
            clarify = "신용점수를 알려주시면 해당 구간 금리로 계산해 드릴게요."
        else:
            kept.append(t)
    if not kept and not clarify:
        clarify = "어떤 상품(정기예금·적금·주담대·전세대출·신용대출)을 비교할지 알려주세요."
    return {"tasks": kept, "clarify": clarify}


# ---------------- 서브에이전트 분배 / 수집 ----------------
def pending(state) -> list[dict]:
    done = {(r["task_id"], r["round"]) for r in state.get("results", [])}
    return [t for t in state.get("tasks", []) if (t["id"], t["round"]) not in done]


def dispatch(state) -> list[Send]:
    """아직 결과가 없는 task만 해당 서브에이전트로 병렬 전송한다."""
    return [Send(f"{t['agent']}_agent", {"task": t, "q": state["query"], "s": state["slots"]}) for t in pending(state)]


def latest(state) -> dict[str, dict]:
    """task별 최신 라운드 결과."""
    out: dict[str, dict] = {}
    for r in state.get("results", []):
        if r["task_id"] not in out or r["round"] >= out[r["task_id"]]["round"]:
            out[r["task_id"]] = r
    return out


@traceable(name="replan_check_node")
async def replan_check_node(state, deps) -> dict:
    """서브에이전트 결과를 모아 본다. 구조화 조회가 0건이면 조건을 한 단계 완화해 다시 보낸다(최대 MAX_REPLAN회)."""
    if state["route"] != "hard":  # easy 경로는 수집만 하고 넘어간다(계약상 ReplanCheck는 hard 전용)
        return {}
    rounds, res = state.get("replan_rounds", 0), latest(state)
    empty = {t["id"] for t in state["tasks"]
             if t["agent"] == "structured" and res[t["id"]]["ok"] and not res[t["id"]]["rows"]}
    if not empty or rounds >= MAX_REPLAN:
        why = "조회 결과 있음" if not empty else f"완화 상한({MAX_REPLAN}회) 도달"
        return {"events": [{"type": "ReplanCheck", "round": rounds, "is_sufficient": True, "reason": why}]}
    tasks, dropped = [dict(t) for t in state["tasks"]], []
    for t in tasks:
        key = next((k for k in ("filters", "term_months", "group") if k in t["args"]), None) if t["id"] in empty else None
        if key:
            t["args"] = {k: v for k, v in t["args"].items() if k != key}
            t["round"] = rounds + 1
            dropped.append(key)
    if not dropped:
        return {"events": [{"type": "ReplanCheck", "round": rounds, "is_sufficient": True,
                            "reason": "더 완화할 조건 없음"}]}
    return {"tasks": tasks, "replan_rounds": rounds + 1,
            "events": [{"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
                        "reason": f"조회 0건 → 조건 완화: {', '.join(dropped)} 제거"}]}


def collected(state) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """(rows, calcs, doc_ids, errors) — task 순서대로."""
    res = latest(state)
    rows, calcs, docs, errors = [], [], [], []
    for t in state.get("tasks", []):
        r = res.get(t["id"])
        if r is None:
            continue
        if not r["ok"]:
            errors.append(f"{t['desc']} 실패: {r['error']}")
        elif t["agent"] == "structured":
            rows += r["rows"]
        elif t["agent"] == "calc":
            calcs.append(r["calc"])
            if r.get("rate_row"):
                rows.append(r["rate_row"])
        else:
            docs += r["doc_ids"]
    docs += [x["product_id"] for x in rows[:3]]
    return rows, calcs, list(dict.fromkeys(docs)), errors


# ---------------- write / validate / finalize ----------------
def product_doc(deps, pid: str) -> ProductDoc:
    p = deps.catalog.by_id[pid]
    end = p.base["dcls_end_day"]
    return ProductDoc(product_id=p.id, title=f"{p.bank} {p.name} ({KIND_KO[p.kind]})",
                      info={"금융회사": p.bank, "금융권": GROUP_KO[p.group], "가입 방법": p.base["join_way"],
                            "판매 상태": "판매중" if not end else f"공시 종료일 {end}"},
                      rate_table=rate_table(p), data_flags=p.flags,
                      descriptions=[Description(section=s, text=t) for s, t in descriptions(p)])


@traceable(name="write_node")
async def write_node(state, deps) -> dict:
    rows, calcs, doc_ids, _ = collected(state)
    inp = AnswerInput(
        question=state["query"],
        rows=[RateRow(**{k: r[k] for k in ("product_id", "bank", "product", "option", "value")},
                      metric=METRIC_KO.get(r["metric"], "대출금리")) for r in rows],
        calculations=[Calculation(**c) for c in calcs],
        documents=[product_doc(deps, i) for i in doc_ids], citable_ids=doc_ids,
        procedure=state.get("procedure"), previous_rejection=state.get("feedback"))
    try:
        out, u = await _llm(deps, WRITE_SYSTEM, inp, AnswerOutput)
        draft = out.model_dump()
    except LLMOutputError as e:  # 숨기지 않고 검증 실패로 기록한다
        draft, u = {"_error": str(e)}, {"llm_calls": 1}
    return {**u, "draft": draft, "answer_input": inp.model_dump()}


@traceable(name="validate_node")
async def validate_node(state, deps) -> dict:
    d, attempts, inp = state["draft"], state.get("attempts", 0) + 1, AnswerInput(**state["answer_input"])
    insufficient, dropped = False, []
    if d.get("_error"):
        ok, why = False, d["_error"]
    elif not d["ok"]:
        ok, why, insufficient = False, f"LLM이 근거 부족으로 판단: {d.get('missing') or '사유 없음'}", True
    elif not d["reply"].strip():
        ok, why = False, "빈 답변"
    else:
        sections = {doc.product_id: "\n".join(x.text for x in doc.descriptions) for doc in inp.documents}
        # 인용은 보조 정보다. 원문과 다른 인용은 답을 반려하지 않고 빼되, 뺀 사실은 경고로 남긴다.
        d = {**d, "citations": (split := split_citations(d["citations"], sections))[0]}
        dropped = split[1]
        # 허용 수치는 구조화된 근거에서만 단위별로 모은다(반려 사유·절차 문구는 근거가 아니다)
        ok, why = check_answer(d["reply"], allowed_values(inp, state["query"]), [], sections,
                               required=[(c.result, c.after_tax) if c.after_tax is not None else (c.result,)
                                         for c in inp.calculations],
                               winner=_winner(state["query"], inp.rows, deps.catalog))
    return {"attempts": attempts, "verified": ok, "feedback": why, "insufficient": insufficient, "draft": d,
            "dropped_citations": dropped,
            "events": [{"type": "Validation", "attempt": attempts, "verdict": "pass" if ok else "fail",
                        "reason": why}]}


def _winner(query: str, rows: list[RateRow], catalog) -> tuple[list[str], list[str]] | None:
    if not rows or not re.search(WINNER_RE, query):
        return None
    specific = lambda name: core_name(name) not in catalog.generic  # 일반명 상품명은 특정 불가라 판정에서 뺀다
    names = lambda rs: [n for r in rs for n in ((r.product,) if specific(r.product) else ()) +
                        (r.bank.replace("주식회사", "").strip(),)]
    return names([r for r in rows if r.value == rows[0].value]), names(rows)


def _card(deps, pid: str, highlight: dict | None = None) -> dict:
    p = deps.catalog.by_id[pid]
    end = p.base["dcls_end_day"]
    return {"id": p.id, "kind": p.kind, "kind_ko": KIND_KO[p.kind], "group": p.group, "bank": p.bank,
            "product": p.name, "sale": "판매중" if not end else f"공시 종료일 {end}", "highlight": highlight,
            "data_flags": p.flags}


def _final(state, deps, reply, products=(), citations=(), calcs=(), extra=()) -> dict:
    y, m = deps.data_month[:4], deps.data_month[4:]
    return {"reply": reply, "route": state["route"], "products": list(products), "citations": list(citations),
            "calculations": [{k: c[k] for k in ("name", "inputs", "formula", "result", "unit")}
                             | ({"after_tax": c["after_tax"]} if c.get("after_tax") is not None else {})
                             for c in calcs],
            "warnings": [WARNING.format(y=y, m=int(m))] + list(extra)}


def _table_reply(rows: list[dict], calcs: list[dict]) -> str:
    out = []
    if rows:
        out.append("공시 데이터 조회 결과예요.")
        out += [f"{i}. {r['bank']} {r['product']} — {r['option']} {METRIC_KO.get(r['metric'], '대출금리')} {r['value']}%"
                for i, r in enumerate(rows[:5], 1)]
    for c in calcs:
        tax = f", 세후 {c['after_tax']:,}원" if c.get("after_tax") is not None else ""
        out.append(f"{c['name']}: {c['result']:,}원{tax} ({c['formula']})")
    return "\n".join(out) or "조건에 맞는 상품을 찾지 못했어요. 조건을 바꿔 다시 물어봐 주세요."


@traceable(name="finalize_node")
async def finalize_node(state, deps) -> dict:
    if state.get("clarify") and not state.get("tasks"):
        return {"final": _final(state, deps, state["clarify"])}
    route = state["route"]
    rows, calcs, doc_ids, extra = collected(state)
    if state.get("clarify"):
        extra.append(state["clarify"])
    cites = []
    if state.get("verified"):
        reply, cites = state["draft"]["reply"], state["draft"]["citations"]
        if state.get("dropped_citations"):
            extra.append(f"원문과 일치하지 않는 인용 {len(state['dropped_citations'])}건은 제외했어요.")
    elif state.get("insufficient"):
        reply = f"공시 자료만으로는 답하기 어려워요. {state['draft'].get('missing') or ''}".strip()
    elif route == "hard":
        reply = _table_reply(rows, calcs)
        extra.append("답변이 검증을 통과하지 못해 도구 결과를 그대로 보여드려요.")
    else:
        names = [deps.catalog.by_id[i].name for i in doc_ids]
        reply = "검증된 답을 만들지 못했어요. 아래 상품의 공시 내용을 직접 확인해 주세요: " + ", ".join(names)
    if route == "hard" and rows:
        products = []
        for r in rows:
            if r["product_id"] not in {p["id"] for p in products} and len(products) < 3:
                products.append(_card(deps, r["product_id"], {
                    "label": f"{r['option']} {METRIC_KO.get(r['metric'], '대출금리')}", "value": r["value"],
                    "unit": "%"}))
    else:
        ids = [c["product_id"].strip(" []") for c in cites if c["product_id"].strip(" []") in deps.catalog.by_id] \
            or doc_ids[:1]
        products = [_card(deps, i) for i in dict.fromkeys(ids)]
    return {"final": _final(state, deps, reply, products, cites, calcs, extra)}


# ---------------- 분기 ----------------
def after_route(state):
    r = state["route"]
    if r == "easy":
        return dispatch(state)
    return {"chat": "chat_answer", "out_of_scope": "refuse", "hard": "plan"}[r]


def after_gate(state):
    return dispatch(state) if state["tasks"] else "finalize"


def after_replan(state):
    return dispatch(state) if pending(state) else "write"


def after_validate(state) -> str:
    if state["verified"] or state.get("insufficient") or state["attempts"] > MAX_REWRITE[state["route"]]:
        return "finalize"
    return "write"
