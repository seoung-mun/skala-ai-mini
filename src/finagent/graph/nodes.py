"""v2 감독(supervisor) 노드. 모두 (state, deps) -> 상태 델타를 돌려주는 async 함수다.

숫자(금리·순위·이자)는 서브에이전트가 MCP 도구로만 만들고, LLM은 구조화된 근거(AnswerInput)를 읽고
AnswerOutput(ok, reply, citations)을 쓰며, 검증은 코드가 한다.
"""

from __future__ import annotations

import asyncio
import re
import time

from langgraph.config import get_stream_writer
from langgraph.types import Send
from langsmith import traceable

from ..catalog import LOAN_KINDS, METRICS, core_name, filters_for
from ..parser import GROUP_KO, JOIN_DENY, KIND_KO, descriptions, rate_table
from ..requirements import (
    build_requirements,
    check_requirements,
    source_citations,
)
from ..schemas import (
    AnswerInput,
    AnswerOutput,
    Calculation,
    ChatInput,
    ChatOutput,
    Description,
    LLMOutputError,
    PlanInput,
    PlanOutput,
    ProductDoc,
    RateRow,
    RewriteInput,
    RewriteOutput,
    RouteInput,
    RouteOutput,
    Turn,
)
from ..slots import FINANCE_RE, Slots, asks_recall, extract, route_rules
from ..validate import (
    allowed_values,
    check_answer,
    condition_rate,
    split_citations,
)
from .state import MAX_REPLAN, MAX_REWRITE

TEXT_CUE = r"우대|조건|가입|대상|수수료|연체|한도|해지|유의|방법|언제|누가"
WINNER_RE = r"제일|가장|더\s*(높|낮|좋|나)|어디|어느|뭐가|1위"
METRIC_KO = {
    "rate": "기본금리",
    "rate_max": "최고금리(우대 포함)",
    "spread": "우대 폭(최고-기본)",
    "min": "최저금리",
    "max": "최고금리",
}


def _unit(metric: str) -> str:
    """metric의 단위: 우대 폭(spread)은 %p, 나머지는 %."""
    return "%p" if metric == "spread" else "%"


ROUTE_SYSTEM = (
    "금융상품 질문 분류기다. 입력의 질문을 읽고 지정된 JSON 형식으로만 답한다."
)
CHAT_SYSTEM = (
    "금융상품 공시 안내 챗봇이다. 인사·잡담에 한국어 한두 문장으로 "
    "답하고, 정기예금·적금·주택담보대출·"
    "전세자금대출·신용대출의 금감원 공시 정보를 비교·계산해 줄 수 있다고 "
    "안내한다. 지정된 JSON 형식으로만 답한다."
)
WRITE_SYSTEM = (
    "금융상품 상담원이다. 입력의 '조회 결과'·'계산 결과'·'문서' 섹션만 "
    "근거로 질문에 답한다.\n"
    "- 금리·금액은 근거의 숫자를 그대로 쓰고 직접 계산하거나 반올림하지 "
    "않는다. 금리에는 % 또는 %p, 금액에는 원 단위를 반드시 붙인다. "
    "계산 결과가 있으면 반드시 쓴다.\n"
    "- 조회 결과가 질문이 묻는 값(기본금리·최고금리·우대 폭)이다. 우대 "
    "폭(%p)을 물으면 그 %p 값을 먼저 답한다.\n"
    "- 개별 우대조건 질문은 해당 조건의 우대율부터 답한다. 기본금리·전체 "
    "우대율·최고금리와 혼동하지 않는다.\n"
    "- 금리를 더하거나 빼는 설명·등식은 쓰지 않는다. 합산 없이 공시에 "
    "적힌 값을 각각 설명한다.\n"
    "- 조회 결과는 1위부터 정렬돼 있다. 1위를 묻는 질문이면 1위부터 "
    "답하고, 동률이면 모두 말한다.\n"
    "- citations는 각 문서의 '설명' 섹션 문장만 글자 그대로 복사한다. "
    "금리표는 인용하지 않는다. "
    "product_id는 '인용 가능한 product_id' 중에서 고르고, 인용할 문장이 "
    "없으면 빈 배열로 둔다.\n"
    "- '수행하지 못한 조사'가 있으면 그 부분은 확인하지 못했다고 짧게 "
    "밝히고 추측하지 않는다.\n"
    "- 근거가 질문에 답하기에 부족하면 ok=false와 missing을 쓴다. 공시 "
    "평균금리는 말하지 않는다.\n"
    "- 가입정보는 '가입 대상: 공시 원문'처럼 항목을 구분하고, 누구나·무조건 등으로 "
    "가입 제한을 일반화하거나 원문을 부정하지 않는다.\n"
    "- reply에는 product_id 같은 내부 식별자를 쓰지 말고 은행명과 "
    "상품명으로 말한다.\n"
    "- reply는 짧은 Markdown 문단으로 쓴다. 여러 상품은 상품마다 '- ' 목록으로 "
    "나누고, 계산 결과는 빈 줄 뒤 별도 문단에 쓴다. 표는 쓰지 않는다. "
    "문서 코드나 '[문서: ...]' 표시는 reply에 쓰지 않는다. "
    "요약과 목록에서 같은 사실을 반복하지 말고 출처 부록을 reply에 붙이지 않는다.\n"
    "- '이전 답변이 반려된 이유'가 있으면 모두 고친다. 지정된 JSON "
    "형식으로만 답한다."
)
PLAN_SYSTEM = (
    "금융상품 질문의 작업 계획자다. 질문을 읽고 필요한 조사 작업만 "
    "고른다. 지정된 JSON 형식으로만 답한다.\n"
    "- structured: 조건에 맞는 상품을 금리 순으로 조회한다. "
    "비교·순위·특정 상품의 금리 수치가 필요할 때. kind 필수.\n"
    "- calc: 이자·월 상환액·목표 금액 역산. 질문에 금액이 있을 때만. "
    "금리를 상품에서 가져와야 하면 그 상품 kind, "
    "질문에 금리가 주어졌으면 none.\n"
    "- rag: 우대조건·가입 대상·수수료·유의사항 같은 설명 원문이 필요할 "
    "때. kind는 none.\n"
    "- kind는 '코드가 뽑은 조건'의 kinds를 우선한다. 금액·기간·점수는 "
    "고르지 않는다(코드가 넣는다).\n"
    "- 작업으로 나눌 수 없는 질문이면 ok=false."
)
REWRITE_SYSTEM = (
    "금융상품 상담 대화의 후속 질문을 독립 질문으로 다시 쓴다. 지정된 "
    "JSON 형식으로만 답한다.\n"
    "- 이번 질문이 직전 대화의 상품·은행·기간·금리 종류(기본·최고·우대 "
    "폭)·금액을 이어받으면 "
    "그것을 채워 넣은 한 문장으로 쓴다. 예: '그거 24개월은?' → '우리은행 "
    "우리SUPER주거래적금 24개월 기본금리 얼마야?'\n"
    "- '그거'가 직전 답변에서 고른 1위 상품을 가리키면 그 상품 하나로 쓴다.\n"
    "- 상품을 바꿔 묻는 질문('신한은행 쏠편한 정기예금은?')은 새 상품에 "
    "직전 조건(기간·금리 종류)을 붙인다.\n"
    "- 직전 답변에만 나온 금리·금액 숫자는 질문에 넣지 않는다. 숫자는 "
    "사용자 질문에 있던 것만 쓴다.\n"
    "- 직전 대화와 무관한 새 질문이면 follow_up=false로 이번 질문을 "
    "그대로 둔다."
)
FOLLOW_RE = r"그거|그건|그게|거기|그럼|그 상품|이거|저거|(은|는)\s*\??\s*$"
REFUSE_REPLY = (
    "죄송해요, 저는 금감원에 공시된 "
    "정기예금·적금·주택담보대출·전세자금대출·신용대출 정보만 안내할 수 "
    "있어요. "
    "이 범위의 상품을 물어봐 주세요."
)
WARNING = (
    "{y}년 {m}월 공시 기준이며 실제 적용 금리는 개인 조건에 따라 다릅니다. "
    "가입·대출 권유가 아닙니다."
)


async def _llm(deps, system: str, payload, out, llm=None, timing=None):
    """구조화 LLM 호출. (출력 모델, 사용량 델타)를 돌려준다. 형식 오류는 LLMOutputError로 올린다."""
    result, u = await asyncio.to_thread(
        (llm or deps.llm).structured, system, payload, out
    )
    if timing is not None:
        timing.update(
            {
                k: u[k]
                for k in (
                    "prompt_tokens",
                    "completion_tokens",
                    "load_ms",
                    "prompt_ms",
                    "gen_ms",
                )
                if k in u
            }
        )
    return result, {
        "llm_calls": 1,
        "prompt_tokens": u["prompt_tokens"],
        "completion_tokens": u["completion_tokens"],
        "cost_usd": u["usd"],
    }


def _add(*deltas: dict) -> dict:
    """LLM 사용량 델타를 합친다(한 노드에서 LLM을 두 번 부를 때)."""
    out: dict = {}
    for d in deltas:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out


def _numbers(text: str) -> set[str]:
    """텍스트의 숫자 문자열 집합(쉼표 제거)."""
    return set(re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")))


async def _rewrite(q: str, state, deps) -> tuple[str, dict, dict | None]:
    """후속 질문을 독립 질문으로 다시 쓴다. (쓸 질문, 사용량 델타, 이벤트용 기록 | None).
    재작성이 직전 답변의 숫자를 질문에 넣으면 버리고 원문을 쓴다(숫자는 사용자 발화에서만 온다)."""
    hist, prev = state.get("history") or [], state.get("prev_slots")
    users = [i for i, m in enumerate(hist) if m["role"] == "user"]
    if not (prev and users):
        return q, {}, None
    s0 = extract(q, deps.catalog)
    if not re.search(FINANCE_RE, q) and not re.search(FOLLOW_RE, q):
        return q, {}, None
    if (s0.kinds or s0.product_ids) and not re.search(
        FOLLOW_RE, q
    ):  # 상품·종류가 있고 지시어도 없으면 독립 질문
        return q, {}, None
    i = users[-1]
    prev_q = hist[i]["content"]
    prev_a = next(
        (m["content"] for m in hist[i + 1 :] if m["role"] == "assistant"), ""
    )[:600]
    try:
        out, delta = await _llm(
            deps,
            REWRITE_SYSTEM,
            RewriteInput(
                previous_question=prev_q, previous_answer=prev_a, question=q
            ),
            RewriteOutput,
        )
    except LLMOutputError as e:
        return (
            q,
            {"llm_calls": 1},
            {
                "original": q,
                "question": q,
                "applied": False,
                "reason": f"재작성 형식 오류: {e}",
            },
        )
    rec = {
        "original": q,
        "question": out.question,
        "applied": False,
        "reason": out.reason,
        "follow_up": out.follow_up,
    }
    if (
        not out.follow_up
        or not out.question.strip()
        or out.question.strip() == q.strip()
    ):
        return q, delta, {**rec, "question": q}
    if bad := sorted(_numbers(out.question) - _numbers(q) - _numbers(prev_q)):
        return (
            q,
            delta,
            {
                **rec,
                "reason": f"사용자 질문에 없는 숫자 {bad}가 들어가 원문 사용",
            },
        )
    return out.question, delta, {**rec, "applied": True}


def _task(
    i: int, agent: str, desc: str, args: dict | None = None, **extra
) -> dict:
    """task dict(id, agent, desc, args, round)를 만든다."""
    return {
        "id": f"t{i}",
        "agent": agent,
        "desc": desc,
        "args": args or {},
        "round": 0,
        **extra,
    }


# ---------------- route ----------------
@traceable(name="route_node")
async def route_node(state, deps) -> dict:
    """질문을 재작성하고 슬롯을 뽑은 뒤 규칙(정하지 못하면 LLM)으로 경로를 정한다.
    대상이 없는 후속 질문은 직전 슬롯을 이어받는다."""
    original = state["query"]
    if asks_recall(original):
        return {
            "route": "chat",
            "intent": "recall",
            "slots": Slots().to_dict(),
            "tasks": [],
            "events": [
                {
                    "type": "RouteDecided",
                    "route": "chat",
                    "intent": "recall",
                    "reason": "저장된 직전 사용자 질문 회상",
                }
            ],
        }
    q, rw_delta, rewrite = await _rewrite(original, state, deps)
    s = extract(q, deps.catalog)
    prev = state.get("prev_slots")
    follow_up = bool(prev) and (
        rewrite.get("follow_up", False)
        if rewrite and "follow_up" in rewrite
        else bool(
            re.search(FOLLOW_RE, original)
            and (
                re.search(FINANCE_RE, original)
                or s.term_months is not None
                or s.amount_won is not None
                or s.credit_score is not None
            )
        )
    )
    if follow_up and not (s.kinds or s.product_ids):
        changed_scope = bool(
            (s.banks and s.banks != prev["banks"])
            or (s.group and s.group != prev.get("group"))
        )
        s.kinds = prev["kinds"]
        s.product_ids = [] if changed_scope else prev["product_ids"]
        s.banks = s.banks or ([] if changed_scope else prev["banks"])
    if follow_up and set(s.kinds) == set(prev["kinds"]):
        s.group = s.group or prev.get("group")
        s.term_months = (
            s.term_months
            if s.term_months is not None
            else prev.get("term_months")
        )
        s.credit_score = (
            s.credit_score
            if s.credit_score is not None
            else prev.get("credit_score")
        )
        s.filters = {**prev.get("filters", {}), **s.filters}
        if any(k in ("saving", "deposit") for k in s.kinds):
            if s.metric == "max":
                s.metric = "rate_max"
            elif "기본" in q:
                s.metric = "rate"
        if s.metric is None and not re.search(TEXT_CUE, q):
            s.metric = prev.get("metric")
    route, reason = route_rules(q, s)
    delta: dict = {}
    if route is None:
        out, delta = await _llm(
            deps, ROUTE_SYSTEM, RouteInput(question=q), RouteOutput
        )
        route, reason = (
            (out.route, f"LLM 분류: {out.reason}")
            if out.ok
            else ("chat", "분류 불가 → 대화로 무엇이 필요한지 묻기")
        )
    tasks = (
        [_task(1, "rag", "우대조건·설명 원문 검색")] if route == "easy" else []
    )
    intent = (
        route
        if route in ("chat", "out_of_scope")
        else "finance_followup"
        if follow_up
        else "finance_new"
    )
    decided = {
        "type": "RouteDecided",
        "route": route,
        "intent": intent,
        "reason": reason,
    } | (
        {
            "rewrite": {
                k: rewrite[k]
                for k in ("original", "question", "applied", "reason")
            }
        }
        if rewrite
        else {}
    )
    rewritten = (
        {"query": q, "original_query": state["query"]}
        if q != state["query"]
        else {}
    )
    return {
        **_add(rw_delta, delta),
        **rewritten,
        "route": route,
        "intent": intent,
        "slots": s.to_dict(),
        "tasks": tasks,
        "events": [decided],
    }


@traceable(name="chat_answer_node")
async def chat_answer_node(state, deps) -> dict:
    """잡담 경로: 최근 대화로 LLM 응답을 만든다."""
    if state.get("intent") == "recall":
        previous = next(
            (
                m["content"]
                for m in reversed(state.get("history", []))
                if m["role"] == "user"
            ),
            None,
        )
        reply = (
            f"직전에는 이렇게 질문하셨어요:\n\n> {previous}"
            if previous
            else "이 대화에는 이전 질문 기록이 없어요."
        )
        return {"final": _final(state, deps, reply)}
    hist = [Turn(**m) for m in state.get("history", [])[-6:]]
    out, u = await _llm(
        deps,
        CHAT_SYSTEM,
        ChatInput(question=state["query"], history=hist),
        ChatOutput,
    )
    reply = (
        out.reply.strip()
        if out.ok and out.reply.strip()
        else "무엇을 도와드릴까요? 예금·적금·대출 상품을 물어봐 주세요."
    )
    return {**u, "final": _final(state, deps, reply)}


@traceable(name="refuse_node")
async def refuse_node(state, deps) -> dict:
    """범위 밖 질문에 고정 거절 문구로 답한다."""
    return {"final": _final(state, deps, REFUSE_REPLY)}


# ---------------- hard: plan / gate ----------------
def _rule_specs(q: str, s: Slots) -> list[tuple[str, str | None]]:
    """규칙 계획기(PLANNER=rule): 슬롯 신호로 (agent, kind) 목록을 만든다. LLM 계획기의 비교 기준이다."""
    specs: list[tuple[str, str | None]] = [
        ("structured", k)
        for k in s.kinds
        if s.compare or (s.product_ids and not s.amount_won)
    ]
    if s.amount_won:
        specs.append(("calc", None))
    if re.search(TEXT_CUE, q) or (s.product_ids and not s.compare):
        specs.append(("rag", None))
    return specs


def plan_gate(
    out: PlanOutput, s: Slots
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """LLM 계획을 코드로 검사한다(이중 게이트의 2단계). 통과한 (agent, kind)와 뺀 이유를 돌려준다.
    LLM은 작업 분해만 정하고, 금액·기간·점수 같은 숫자는 코드가 뽑은 슬롯에서만 가져온다."""
    kept: list[tuple[str, str | None]] = []
    removed: list[str] = []
    for t in out.tasks:
        kind = None if t.kind == "none" else t.kind
        if t.agent == "structured" and kind is None:
            removed.append("structured: 상품 종류 없음")
        elif t.agent == "structured" and s.kinds and kind not in s.kinds:
            removed.append(f"structured: 질문에 없는 종류 {kind}")
        elif t.agent == "calc" and not s.amount_won:
            removed.append("calc: 질문에 금액 없음")
        elif t.agent == "calc" and any(a == "calc" for a, _ in kept):
            removed.append("calc: 중복")
        else:
            spec = (t.agent, kind if t.agent != "rag" else None)
            if spec in kept:
                removed.append(f"{t.agent}: 중복")
            else:
                kept.append(spec)
    order = {"structured": 0, "calc": 1, "rag": 2}
    return sorted(kept, key=lambda x: order[x[0]]), removed


async def _llm_specs(
    q: str, s: Slots, deps
) -> tuple[list[tuple[str, str | None]] | None, dict, str, list[str]]:
    """LLM 계획기. 실패·거절·전부 제거되면 None을 돌려 호출자가 규칙 계획으로 바꾸고 그 사실을 이벤트에 남긴다."""
    try:
        slots = {
            **s.to_dict(),
            "product_ids": [
                f"{deps.catalog.by_id[i].bank} {deps.catalog.by_id[i].name}"
                for i in s.product_ids
            ],
        }  # 내부 id 대신 모델이 읽을 수 있는 이름
        out, delta = await _llm(
            deps,
            PLAN_SYSTEM,
            PlanInput(question=q, slots=slots),
            PlanOutput,
            deps.agent,
        )
    except LLMOutputError as e:
        return (
            None,
            {"llm_calls": 1},
            f"LLM 계획 형식 오류 → 규칙 계획 사용: {e}",
            [],
        )
    if not out.ok:
        return (
            None,
            delta,
            f"LLM이 계획 불가로 판단 → 규칙 계획 사용: {out.reason}",
            [],
        )
    specs, removed = plan_gate(out, s)
    if not specs:
        return (
            None,
            delta,
            f"LLM 계획이 게이트에서 모두 제거됨 → 규칙 계획 사용: {out.reason}",
            removed,
        )
    return specs, delta, out.reason, removed


@traceable(name="plan_node")
async def plan_node(state, deps) -> dict:
    """hard 질문을 서브에이전트 task로 나눈다(PLANNER=llm이면 LLM 계획+게이트, 실패하면 규칙 계획)."""
    q, s = state["query"], Slots(**state["slots"])
    m = re.search(r"(\d+)\s*개(?!월)", q)  # '12개월'은 개수가 아니다
    limit = int(m.group(1)) if m else 5

    def query_args(kind: str) -> dict:
        """그 종류에 적용되는 인자만 담은 query_products 인자를 만든다."""
        # 종류에 적용되지 않는 인자는 넘기지 않는다(도구는 해당 없는 인자를 오류로 돌려준다)
        metric = s.metric if s.metric in METRICS[kind] else None
        args = {
            "kind": kind,
            "group": s.group,
            "term_months": None if kind in LOAN_KINDS else s.term_months,
            "metric": metric,
            "credit_score": s.credit_score if kind == "credit" else None,
            "filters": filters_for(kind, s.filters) or None,
            "limit": limit,
        }
        ids = [i for i in s.product_ids if deps.catalog.by_id[i].kind == kind]
        if ids:
            args["product_ids"] = ids
        elif s.banks:
            args["banks"] = s.banks
        return {k: v for k, v in args.items() if v is not None}

    delta: dict = {}
    planner, reason, removed = "rule", "슬롯 규칙", []
    specs = None
    if deps.planner == "llm":
        specs, delta, reason, removed = await _llm_specs(q, s, deps)
        if specs is not None:
            planner = "llm"
    if specs is None:
        specs = _rule_specs(q, s)
    # 명시적 조회·계산 요청은 계획자가 빠뜨려도 실행 계획에 남긴다.
    required = [item for item in _rule_specs(q, s) if item[0] != "rag"]
    if m and limit > 1:
        required += [("structured", kind) for kind in s.kinds]
    added = []
    for agent, kind in required:
        if not any(
            a == agent and (agent == "calc" or k == kind) for a, k in specs
        ):
            specs.append((agent, kind))
            added.append(agent)
    if added:
        reason += f" · 코드가 누락된 필수 작업 추가: {', '.join(added)}"

    tasks: list[dict] = []
    for agent, kind in specs:
        if agent == "structured":
            a = query_args(kind)
            tasks.append(
                _task(
                    len(tasks) + 1,
                    "structured",
                    f"{KIND_KO[kind]} {METRIC_KO.get(a.get('metric', ''), '금리')} 조회",
                    a,
                )
            )
        elif agent == "calc":
            loan = (
                (kind in LOAN_KINDS)
                if kind
                else (
                    any(k in LOAN_KINDS for k in s.kinds)
                    or bool(re.search(r"빌리|빌려|대출|갚|상환", q))
                )
            )
            monthly = s.monthly or bool(
                re.search(r"매달|매월|월\s*얼마|한\s*달에", q)
            )
            ptype = (
                "loan"
                if loan
                else (
                    "saving"
                    if (kind == "saving" or "saving" in s.kinds or monthly)
                    else "deposit"
                )
            )
            args = {
                "product_type": ptype,
                "amount_won": s.amount_won,
                "term_months": s.term_months,
                "rate_pct": s.rate_pct,
                "mode": "goal" if s.goal else "forward",
            }
            if ptype == "loan" and "만기일시" in q:
                args["repayment"] = "bullet"
            elif ptype != "loan" and "복리" in q:
                args["interest_type"] = "compound"
            rate_kind = kind or next(
                (k for k in s.kinds if (k in LOAN_KINDS) == loan), None
            )
            tasks.append(
                _task(
                    len(tasks) + 1,
                    "calc",
                    f"{ptype} {'역산' if s.goal else '계산'}",
                    args,
                    rate_query=query_args(rate_kind) if rate_kind else None,
                )
            )
        else:
            tasks.append(
                _task(len(tasks) + 1, "rag", "우대조건·설명 원문 검색")
            )
    procedure = await deps.mcp.get_prompt("compare_procedure", {"question": q})
    return {
        **delta,
        "tasks": tasks,
        "procedure": procedure,
        "replan_rounds": 0,
        "events": [
            {
                "type": "PlanCreated",
                "planner": planner,
                "reason": reason,
                "removed": removed,
                "tasks": [
                    {k: t[k] for k in ("id", "agent", "desc")} for t in tasks
                ],
            }
        ],
    }


@traceable(name="gate_node")
async def gate_node(state, deps) -> dict:
    """계획을 코드로 검사한다(베이스 권위 게이트). 실행 불가능한 task는 빼고, 필요한 정보는 되묻는다."""
    kept, clarify = [], None
    for t in state["tasks"]:
        a, rq = t["args"], t.get("rate_query") or {}
        if (
            t["agent"] == "structured"
            and a["kind"] == "credit"
            and a.get("credit_score") is None
        ):
            clarify = (
                "신용대출 금리는 신용점수 구간마다 달라요. 신용점수(예: "
                "750점)를 알려주시면 비교해 드릴게요."
            )
        elif t["agent"] == "calc" and a.get("term_months") is None:
            clarify = "기간(예: 12개월, 30년)을 알려주시면 계산해 드릴게요."
        elif t["agent"] == "calc" and a.get("rate_pct") is None and not rq:
            clarify = (
                "금리를 알려주시거나 상품을 지정해 주시면 계산해 드릴게요."
            )
        elif (
            t["agent"] == "calc"
            and a.get("rate_pct") is None
            and rq.get("kind") == "credit"
            and rq.get("credit_score") is None
        ):
            clarify = "신용점수를 알려주시면 해당 구간 금리로 계산해 드릴게요."
        else:
            kept.append(t)
    if not kept and not clarify:
        clarify = (
            "어떤 상품(정기예금·적금·주담대·전세대출·신용대출)을 비교할지 "
            "알려주세요."
        )
    return {"tasks": kept, "clarify": clarify}


# ---------------- 서브에이전트 분배 / 수집 ----------------
def pending(state) -> list[dict]:
    """아직 결과가 없는 (task, round)의 task 목록."""
    done = {(r["task_id"], r["round"]) for r in state.get("results", [])}
    return [
        t for t in state.get("tasks", []) if (t["id"], t["round"]) not in done
    ]


def dispatch(state) -> list[Send]:
    """아직 결과가 없는 task만 해당 서브에이전트로 병렬 전송한다."""
    return [
        Send(
            f"{t['agent']}_agent",
            {"task": t, "q": state["query"], "s": state["slots"]},
        )
        for t in pending(state)
    ]


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
    if (
        state["route"] != "hard"
    ):  # easy 경로는 이벤트 없이 수집만 한다(계약상 ReplanCheck는 hard 전용)
        return _rate_followup(state, deps)
    rounds, res = state.get("replan_rounds", 0), latest(state)
    empty = {
        t["id"]
        for t in state["tasks"]
        if t["agent"] == "structured"
        and res[t["id"]]["ok"]
        and not res[t["id"]]["rows"]
    }
    if not empty or rounds >= MAX_REPLAN:
        why = (
            "조회 결과 있음" if not empty else f"완화 상한({MAX_REPLAN}회) 도달"
        )
        return {
            "events": [
                {
                    "type": "ReplanCheck",
                    "round": rounds,
                    "is_sufficient": True,
                    "reason": why,
                }
            ]
        }
    tasks, dropped = [dict(t) for t in state["tasks"]], []
    for t in tasks:
        key = (
            next(
                (
                    k
                    for k in ("filters", "term_months", "group")
                    if k in t["args"]
                ),
                None,
            )
            if t["id"] in empty
            else None
        )
        if key:
            t["args"] = {k: v for k, v in t["args"].items() if k != key}
            t["round"] = rounds + 1
            dropped.append(key)
    if not dropped:
        return {
            "events": [
                {
                    "type": "ReplanCheck",
                    "round": rounds,
                    "is_sufficient": True,
                    "reason": "더 완화할 조건 없음",
                }
            ]
        }
    return {
        "tasks": tasks,
        "replan_rounds": rounds + 1,
        "events": [
            {
                "type": "ReplanCheck",
                "round": rounds,
                "is_sufficient": False,
                "reason": f"조회 0건 → 조건 완화: {', '.join(dropped)} 제거",
            }
        ],
    }


def _rate_followup(state, deps) -> dict:
    """easy 금리 질문도 기존 structured 서브그래프로 기간·종류·점수에 맞는 값을 조회한다."""
    s, tasks = Slots(**state["slots"]), state["tasks"]
    numeric = (
        s.metric in ("spread", "rate", "min", "max")
        or s.credit_score is not None
        or (
            s.metric == "rate_max"
            and bool(re.search(r"최고|최대\s*금리", state["query"]))
        )
    )
    if not numeric or any(t["agent"] == "structured" for t in tasks):
        return {}
    pid = (s.product_ids or collected(state)[2] or [None])[0]
    if pid is None:
        return {}
    p = deps.catalog.by_id[pid]
    a = {
        "kind": p.kind,
        "product_ids": [pid],
        "filters": filters_for(p.kind, s.filters),
    }
    if p.kind == "credit":
        a["credit_score"] = s.credit_score
    elif s.metric in METRICS[p.kind]:
        a["metric"] = s.metric
    if s.term_months is not None and p.kind not in LOAN_KINDS:
        a["term_months"] = s.term_months
    return {
        "tasks": tasks
        + [
            _task(
                len(tasks) + 1, "structured", f"{KIND_KO[p.kind]} 금리 조회", a
            )
        ]
    }


def collected(state) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """(rows, calcs, doc_ids, failed) — task 순서대로. failed는 실패한 작업 설명이다.
    오류 원문은 TaskCompleted 이벤트·추적에만 남기고 사용자·답변 LLM에는 무엇을 못 했는지만 보인다."""
    res = latest(state)
    rows, calcs, docs, errors = [], [], [], []
    for t in state.get("tasks", []):
        r = res.get(t["id"])
        if r is None:
            continue
        if not r["ok"]:
            errors.append(t["desc"])
        elif t["agent"] == "structured":
            rows += r["rows"]
        elif t["agent"] == "calc":
            calcs.append(r["calc"])
            if r.get("rate_row"):
                rows.append(r["rate_row"])
        else:
            docs += r["doc_ids"]
    rows = list(
        {
            (r["product_id"], r["metric"], r["option"], r["value"]): r
            for r in rows
        }.values()
    )
    docs += [x["product_id"] for x in rows]
    return rows, calcs, list(dict.fromkeys(docs)), errors


# ---------------- write / validate / finalize ----------------
def product_doc(deps, pid: str) -> ProductDoc:
    """상품을 답변 LLM용 ProductDoc(기본 정보·금리표·설명)으로 만든다."""
    p = deps.catalog.by_id[pid]
    end = p.base["dcls_end_day"]
    return ProductDoc(
        product_id=p.id,
        title=f"{p.bank} {p.name} ({KIND_KO[p.kind]})",
        info={
            "금융회사": p.bank,
            "상품명": p.name,
            "금융권": GROUP_KO[p.group],
            "가입 방법": p.base["join_way"],
            "가입 제한": JOIN_DENY.get(p.base.get("join_deny"), ""),
            "판매 상태": "판매중" if not end else f"공시 종료일 {end}",
        },
        rate_table=rate_table(p),
        data_flags=p.flags,
        descriptions=[
            Description(section=s, text=t) for s, t in descriptions(p)
        ],
    )


@traceable(name="write_node")
async def write_node(state, deps) -> dict:
    """모은 근거로 AnswerInput을 만들어 답변 LLM을 부른다. 형식 오류는 draft에 기록한다."""
    writer, started = get_stream_writer(), time.perf_counter()
    attempt = state.get("attempts", 0) + 1
    writer({"type": "AnswerProgress", "stage": "writing", "attempt": attempt})
    rows, calcs, doc_ids, failed = collected(state)
    if rows and state.get("slots", {}).get("compare"):
        targets = {r["product_id"] for r in rows} | set(
            state["slots"].get("product_ids") or []
        )
        doc_ids = [pid for pid in doc_ids if pid in targets]
    inp = AnswerInput(
        question=state["query"],
        rows=[
            RateRow(
                **{
                    k: r[k]
                    for k in (
                        "product_id",
                        "bank",
                        "product",
                        "option",
                        "value",
                    )
                },
                metric=METRIC_KO.get(r["metric"], "대출금리"),
                unit=_unit(r["metric"]),
            )
            for r in rows
        ],
        calculations=[Calculation(**c) for c in calcs],
        documents=[product_doc(deps, i) for i in doc_ids],
        citable_ids=doc_ids,
        failed=failed,
        procedure=state.get("procedure"),
        previous_rejection=state.get("feedback"),
    )
    inp.required_facts = build_requirements(inp)
    target = (
        condition_rate(state["query"], inp.documents)
        if not inp.calculations
        else None
    )
    if target:
        label, vals = target
        inp.procedure = (
            (inp.procedure or "")
            + f"\n질문은 {label}입니다. 해당 조건의 공시 우대율 {next(iter(vals)):g}%p부터 답하세요. 합산하지 마세요."
        )
    timing = {"attempt": attempt}
    try:
        out, u = await _llm(
            deps, WRITE_SYSTEM, inp, AnswerOutput, timing=timing
        )
        draft = out.model_dump()
    except LLMOutputError as e:  # 숨기지 않고 검증 실패로 기록한다
        draft, u = {"_error": str(e)}, {"llm_calls": 1}
    timing["writing_ms"] = int((time.perf_counter() - started) * 1000)
    writer(
        {
            "type": "AnswerProgress",
            "stage": "validating",
            "attempt": attempt,
            "writing_ms": int((time.perf_counter() - started) * 1000),
        }
    )
    return {
        **u,
        "draft": draft,
        "answer_input": inp.model_dump(),
        "writing_runs": [*state.get("writing_runs", []), timing],
    }


@traceable(name="validate_node")
async def validate_node(state, deps) -> dict:
    """초안의 수치·인용·1위·묻는 속성을 코드로 검증한다. 원문과 다른 인용은 빼고 경고로 남긴다."""
    d, attempts, inp = (
        state["draft"],
        state.get("attempts", 0) + 1,
        AnswerInput(**state["answer_input"]),
    )
    insufficient, dropped = False, []
    if d.get("_error"):
        ok, why = False, d["_error"]
    elif not d["ok"]:
        ok, why, insufficient = (
            False,
            f"LLM이 근거 부족으로 판단: {d.get('missing') or '사유 없음'}",
            True,
        )
    elif not d["reply"].strip():
        ok, why = False, "빈 답변"
    else:
        sections = {
            doc.product_id: "\n".join(
                [
                    *(x.text for x in doc.descriptions),
                    *(str(v) for v in doc.info.values()),
                ]
            )
            for doc in inp.documents
        }
        # 인용은 보조 정보다. 원문과 다른 인용은 답을 반려하지 않고 빼되, 뺀 사실은 경고로 남긴다.
        d = {
            **d,
            "citations": (
                split := split_citations(
                    source_citations(inp, d["citations"]),
                    sections,
                    {
                        doc.product_id: {
                            **doc.info,
                            **{x.section: x.text for x in doc.descriptions},
                        }
                        for doc in inp.documents
                    },
                )
            )[0],
        }
        dropped = split[1]
        # 허용 수치는 구조화된 근거에서만 단위별로 모은다(반려 사유·절차 문구는 근거가 아니다)
        ok, why = check_answer(
            d["reply"],
            allowed_values(inp, state["query"]),
            [],
            sections,
            required=[
                (c.result, c.after_tax)
                if c.after_tax is not None
                else (c.result,)
                for c in inp.calculations
            ],
            winner=_winner(state["query"], inp.rows, deps.catalog),
            attribute=(
                condition_rate(state["query"], inp.documents)
                or _attribute(state["slots"], inp.rows)
            )
            if not inp.calculations
            else None,
        )
        missing = check_requirements(inp, d["reply"])
        if missing:
            ok, why = False, "\n".join(([why] if why else []) + missing)
    return {
        "attempts": attempts,
        "verified": ok,
        "feedback": why,
        "insufficient": insufficient,
        "draft": d,
        "dropped_citations": dropped,
        "events": [
            {
                "type": "Validation",
                "attempt": attempts,
                "verdict": "pass" if ok else "fail",
                "reason": why,
            }
        ],
    }


def _attribute(
    slots: dict, rows: list[RateRow]
) -> tuple[str, set[float]] | None:
    """질문의 기간·금리 속성·점수로 조회한 값만 첫 금리 주장으로 허용한다."""
    vals = {round(r.value, 2) for r in rows}
    return (
        (METRIC_KO.get(slots.get("metric"), "해당 조건의 대출금리"), vals)
        if vals
        and (slots.get("metric") or slots.get("credit_score") is not None)
        else None
    )


def _winner(
    query: str, rows: list[RateRow], catalog
) -> tuple[list[str], list[str]] | None:
    """1위를 묻는 질문이면 (1위 이름들, 전체 후보 이름들)을 돌려준다. 아니면 None."""
    if not rows or not re.search(WINNER_RE, query):
        return None

    def specific(name):
        return (
            core_name(name) not in catalog.generic
        )  # 일반명 상품명은 특정 불가라 판정에서 뺀다

    def names(rs):
        return [
            n
            for r in rs
            for n in ((r.product,) if specific(r.product) else ())
            + (r.bank.replace("주식회사", "").strip(),)
        ]

    return names([r for r in rows if r.value == rows[0].value]), names(rows)


def _card(deps, pid: str, highlight: dict | None = None) -> dict:
    """응답 products에 넣을 상품 카드 dict를 만든다."""
    p = deps.catalog.by_id[pid]
    end = p.base["dcls_end_day"]
    return {
        "id": p.id,
        "kind": p.kind,
        "kind_ko": KIND_KO[p.kind],
        "group": p.group,
        "bank": p.bank,
        "product": p.name,
        "sale": "판매중" if not end else f"공시 종료일 {end}",
        "highlight": highlight,
        "data_flags": p.flags,
    }


def _final(
    state, deps, reply, products=(), citations=(), calcs=(), extra=()
) -> dict:
    """계약 형식 FinalAnswer 본문(답·경로·상품·인용·계산·경고)을 만든다."""
    y, m = deps.data_month[:4], deps.data_month[4:]
    return {
        "reply": reply,
        "route": state["route"],
        "products": list(products),
        "citations": list(citations),
        "calculations": [
            {k: c[k] for k in ("name", "inputs", "formula", "result", "unit")}
            | (
                {"after_tax": c["after_tax"]}
                if c.get("after_tax") is not None
                else {}
            )
            for c in calcs
        ],
        "warnings": [WARNING.format(y=y, m=int(m))] + list(extra),
    }


def _table_reply(rows: list[dict], calcs: list[dict]) -> str:
    """검증 실패 시 대체 답: 도구 결과(조회 행·계산)를 그대로 나열한다."""
    out = []
    if rows:
        out.append("공시 데이터 조회 결과예요.")
        out += [
            f"{i}. {r['bank']} {r['product']} — {r['option']} {METRIC_KO.get(r['metric'], '대출금리')} {r['value']}{_unit(r['metric'])}"
            for i, r in enumerate(rows, 1)
        ]
    for c in calcs:
        tax = (
            f", 세후 {c['after_tax']:,}원"
            if c.get("after_tax") is not None
            else ""
        )
        out.append(f"{c['name']}: {c['result']:,}원{tax} ({c['formula']})")
    return (
        "\n".join(out)
        or "조건에 맞는 상품을 찾지 못했어요. 조건을 바꿔 다시 물어봐 주세요."
    )


@traceable(name="finalize_node")
async def finalize_node(state, deps) -> dict:
    """검증 결과에 따라 최종 답(검증된 답·근거 부족·도구 결과·되묻기)과 상품 카드를 정한다."""
    if state.get("clarify") and not state.get("tasks"):
        return {"final": _final(state, deps, state["clarify"])}
    route = state["route"]
    rows, calcs, doc_ids, failed = collected(state)
    extra = [f"{d}에 실패해 이 부분은 답에서 빠졌어요." for d in failed]
    if state.get("clarify"):
        extra.append(state["clarify"])
    cites = []
    if state.get("verified"):
        reply, cites = state["draft"]["reply"], state["draft"]["citations"]
        if state.get("dropped_citations"):
            extra.append(
                f"원문과 일치하지 않는 인용 {len(state['dropped_citations'])}건은 제외했어요."
            )
    elif state.get("insufficient"):
        reply = f"공시 자료만으로는 답하기 어려워요. {state['draft'].get('missing') or ''}".strip()
    elif route == "hard" or rows:
        reply = _table_reply(rows, calcs)
        inp = AnswerInput(**state["answer_input"])
        details = [
            f"- {f.product} · {f.section}: {f.value or '공시 정보가 없어 확인 불가'}"
            for f in inp.required_facts
            if f.kind == "text"
        ]
        if details:
            reply += "\n\n공시 원문 항목:\n" + "\n".join(details)
        extra.append(
            "답변이 검증을 통과하지 못해 도구 결과를 그대로 보여드려요."
        )
    else:
        names = [deps.catalog.by_id[i].name for i in doc_ids]
        reply = (
            "검증된 답을 만들지 못했어요. 아래 상품의 공시 내용을 직접 "
            "확인해 주세요: " + ", ".join(names)
        )
    if rows:
        products = []
        for r in rows:
            if r["product_id"] not in {p["id"] for p in products}:
                products.append(
                    _card(
                        deps,
                        r["product_id"],
                        {
                            "label": f"{r['option']} {METRIC_KO.get(r['metric'], '대출금리')}",
                            "value": r["value"],
                            "unit": _unit(r["metric"]),
                        },
                    )
                )
    else:
        ids = [
            c["product_id"].strip(" []")
            for c in cites
            if c["product_id"].strip(" []") in deps.catalog.by_id
        ] or doc_ids[:1]
        products = [_card(deps, i) for i in dict.fromkeys(ids)]
    return {"final": _final(state, deps, reply, products, cites, calcs, extra)}


# ---------------- 분기 ----------------
def after_route(state):
    """route 다음 분기: easy는 서브에이전트로, 나머지는 경로에 맞는 노드로 보낸다."""
    r = state["route"]
    if r == "easy":
        return dispatch(state)
    return {"chat": "chat_answer", "out_of_scope": "refuse", "hard": "plan"}[r]


def after_gate(state):
    """gate 다음 분기: 남은 task가 있으면 서브에이전트로, 없으면 finalize."""
    return dispatch(state) if state["tasks"] else "finalize"


def after_replan(state):
    """replan_check 다음 분기: 결과 없는 task가 있으면 다시 보내고, 없으면 write."""
    return dispatch(state) if pending(state) else "write"


def after_validate(state) -> str:
    """검증 통과·근거 부족·재작성 한도 초과면 finalize, 아니면 다시 write."""
    if (
        state["verified"]
        or state.get("insufficient")
        or state["attempts"] > MAX_REWRITE[state["route"]]
    ):
        return "finalize"
    return "write"
