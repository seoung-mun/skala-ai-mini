"""사용자 조건으로 작업을 계획하고 실행 가능한 인자를 검사한다."""

from __future__ import annotations

from langsmith import traceable

from ..catalog import LOAN_KINDS, METRIC_KO, METRICS, filters_for
from ..parser import KIND_KO
from ..schemas import LLMOutputError, PlanInput, PlanOutput
from ..slots import Slots
from .calls import _failed_usage, _llm

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
    if s.docs or (s.product_ids and not s.compare):
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
        elif (
            t.agent in ("structured", "calc")
            and kind
            and s.kinds
            and kind not in s.kinds
        ):
            removed.append(f"{t.agent}: 질문에 없는 종류 {kind}")
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
            **{k: v for k, v in s.to_dict().items() if k != "question"},
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
            _failed_usage(e),
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
    limit = s.count or 5

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
            "rank": s.rank,
            "exclude_ids": [
                i for i in s.exclude_ids if deps.catalog.by_id[i].kind == kind
            ]
            or None,
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
    if s.count and s.count > 1:
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
            # '그거랑 2위 비교'는 지정 상품 조회와 시장 순위 조회를 따로 실행해 함께 보여준다.
            split = "product_ids" in a and a.get("rank") is not None
            for args in (
                [
                    {k: v for k, v in a.items() if k != "rank"},
                    {k: v for k, v in a.items() if k != "product_ids"},
                ]
                if split
                else [a]
            ):
                tasks.append(
                    _task(
                        len(tasks) + 1,
                        "structured",
                        f"{KIND_KO[kind]} {METRIC_KO.get(args.get('metric', ''), '금리')} 조회",
                        args,
                    )
                )
        elif agent == "calc":
            # 계산 유형은 사용자 슬롯으로 확정하고 LLM 종류 제안에 맡기지 않는다.
            kind = next(iter(s.kinds), None)
            loan = kind in LOAN_KINDS
            monthly = s.monthly
            ptype = (
                "loan"
                if loan
                else (
                    "saving"
                    if kind == "saving" or (kind is None and monthly)
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
            if ptype == "loan" and s.repayment in ("bullet", "equal_payment"):
                args["repayment"] = s.repayment
            elif ptype != "loan" and s.filters.get("rate_type_nm"):
                args["interest_type"] = (
                    "compound"
                    if s.filters["rate_type_nm"] == "복리"
                    else "simple"
                )
            rate_kind = kind or next(
                (k for k in s.kinds if (k in LOAN_KINDS) == loan), None
            )
            rate_query = query_args(rate_kind) if rate_kind else None
            if rate_query and rate_query.get("metric") == "spread":
                rate_query["metric"] = "rate"
            if rate_query and args.get("interest_type"):
                rate_query["filters"] = {
                    **rate_query.get("filters", {}),
                    "rate_type_nm": "복리"
                    if args["interest_type"] == "compound"
                    else "단리",
                }
            tasks.append(
                _task(
                    len(tasks) + 1,
                    "calc",
                    f"{ptype} {'역산' if s.goal else '계산'}",
                    args,
                    rate_query=rate_query,
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
    kept, clarify, s = [], None, state["slots"]
    for t in state["tasks"]:
        a, rq = t["args"], t.get("rate_query") or {}
        if (
            t["agent"] == "calc"
            and a["product_type"] != "loan"
            and s.get("monthly_payout")
        ):
            clarify = "현재 계산은 만기까지의 총이자를 지원합니다. 월별 지급 이자는 지급 조건이 필요해 계산할 수 없어요. 만기 총이자로 계산할지 알려주세요."
        elif t["agent"] == "calc" and s.get("repayment") == "equal_principal":
            clarify = "원금균등 상환 계산은 지원하지 않습니다. 원리금균등 또는 만기일시 방식으로 계산할지 알려주세요."
        elif (
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
