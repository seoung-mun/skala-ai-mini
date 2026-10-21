"""도구 호출 점검 (LLM 없이). 골든 전 문항을 실제 MCP 서버(stdio)·실제 상품 데이터로 그래프에 통과시켜
코드가 만든 도구 인자가 도구 계약을 통과하는지, 도구별 성공률·오류·지연을 잰다.

LLM 자리는 코드 스텁이 채운다(라우팅 실패 시 골든 difficulty로 분기, 계획은 규칙 계획, 답 작성은 '근거 부족').
검색기는 골든 정답 상품을 돌려주는 스텁이라 검색 품질은 재지 않는다 — 검색·답변 품질은 run_eval이 잰다.

    uv run python eval/tool_check.py            # 표 출력 + eval/results/tool_check.json
"""

import asyncio
import json
import statistics as st
import sys
import time
from collections import defaultdict
from pathlib import Path

from finagent.catalog import Catalog
from finagent.deps import Deps
from finagent.graph.build import build_graph, run_v2
from finagent.mcp_client import MCPClient
from finagent.parser import parse_all
from finagent.schemas import (
    AnswerOutput,
    ChatOutput,
    PlanOutput,
    RewriteOutput,
    RouteOutput,
)

ROOT = Path(__file__).resolve().parents[1]


class StubLLM:
    """LLM 없이 그래프를 끝까지 돌리기 위한 코드 응답. 도구 호출 경로만 보려는 것이라 답은 늘 '근거 부족'."""

    route = "easy"

    def structured(self, system, payload, out):
        """출력 모델별 고정 응답을 돌려준다(LLM 호출 없음)."""
        zero = {"prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0}
        if out is RouteOutput:
            return RouteOutput(
                ok=True, route=self.route, reason="스텁: 골든 difficulty"
            ), zero
        if out is PlanOutput:
            return PlanOutput(
                ok=False, tasks=[], reason="스텁: 규칙 계획 사용"
            ), zero
        if out is RewriteOutput:
            return RewriteOutput(
                follow_up=False,
                question=payload.question,
                reason="스텁: 재작성 안 함",
            ), zero
        if out is ChatOutput:
            return ChatOutput(ok=True, reply="안녕하세요"), zero
        if out is AnswerOutput:
            return AnswerOutput(
                ok=False, reply="", citations=[], missing="오프라인 도구 점검"
            ), zero
        raise TypeError(f"스텁이 모르는 출력 모델: {out.__name__}")


class GoldRetriever:
    """골든 정답 상품 id를 그대로 돌려주는 가짜 검색기."""

    def __init__(self):
        """빈 정답 목록으로 시작한다(문항마다 ids를 채운다)."""
        self.ids: list[str] = []

    def search(self, q, k=3, allowed=None):
        """정답 id 중 허용 범위 안의 것을 k개까지 돌려준다."""
        return [(i, 1.0) for i in self.ids if allowed is None or i in allowed][
            :k
        ]


class RecordingMCP:
    """실제 MCP 클라이언트를 감싸 호출마다 (도구, 성공, 오류, 재시도, 지연)을 남긴다."""

    def __init__(self, inner: MCPClient):
        """실제 MCP 클라이언트를 감싸고 호출 기록을 비운다."""
        self.inner, self.calls, self.item = inner, [], None

    async def call_tool(self, name, args):
        """실제 도구를 호출하고 결과·재시도·지연·인자를 기록한다."""
        t0 = time.perf_counter()
        res, retries = await self.inner.call_tool(name, args)
        self.calls.append(
            {
                "item": self.item,
                "tool": name,
                "ok": res.ok,
                "error": res.error,
                "retries": retries,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "args": args,
                "rows": len(res.data["rows"])
                if res.ok and name == "query_products"
                else None,
            }
        )
        return res, retries

    async def _timed(self, tool, coro):
        """코루틴을 실행하며 성공·실패·지연을 기록한다(예외는 그대로 올린다)."""
        t0 = time.perf_counter()
        try:
            out = await coro
            self.calls.append(
                {
                    "item": self.item,
                    "tool": tool,
                    "ok": True,
                    "error": None,
                    "retries": 0,
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                }
            )
            return out
        except Exception as e:  # 기록하고 그대로 올린다(삼키지 않음)
            self.calls.append(
                {
                    "item": self.item,
                    "tool": tool,
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "retries": 0,
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                }
            )
            raise

    async def read_resource(self, uri):
        """Resource 읽기를 기록하며 실행한다."""
        return await self._timed("read_resource", self.inner.read_resource(uri))

    async def get_prompt(self, name, args):
        """Prompt 조회를 기록하며 실행한다."""
        return await self._timed(
            "get_prompt", self.inner.get_prompt(name, args)
        )


# 잘못된 호출에 도구가 돌려주는 오류 문구 — 호출자(코드·에이전트)가 무엇을 고쳐야 하는지 알 수 있어야 한다
BAD_CALLS = [
    ("query_products", {"kind": "loan"}),
    ("query_products", {"kind": "rent", "term_months": 24}),
    ("query_products", {"kind": "deposit", "banks": ["카카오뱅크"]}),
    (
        "query_products",
        {"kind": "deposit", "filters": {"rpay_type_nm": "분할상환방식"}},
    ),
    ("query_products", {"kind": "credit"}),
    ("query_products", {"kind": "saving", "limit": 0}),
    (
        "calculate",
        {
            "product_type": "loan",
            "amount_won": 1000,
            "term_months": 12,
            "rate_pct": 4.0,
            "interest_type": "compound",
        },
    ),
    (
        "calculate",
        {
            "product_type": "saving",
            "amount_won": 0,
            "term_months": 12,
            "rate_pct": 3.0,
        },
    ),
    (
        "calculate",
        {
            "product_type": "deposit",
            "amount_won": 1000000,
            "term_months": 12,
            "rate_pct": 0.035,
        },
    ),
]


async def main() -> int:
    """골든 전 문항을 그래프에 통과시켜 도구 호출을 점검하고 결과를 출력·저장한다. 종료 코드를 돌려준다."""
    golden = [
        json.loads(line)
        for line in (ROOT / "eval/golden.jsonl").read_text().splitlines()
    ]
    inner = MCPClient()
    await inner.start()
    mcp, llm, retr = RecordingMCP(inner), StubLLM(), GoldRetriever()
    deps = Deps(
        llm=llm,
        mcp=mcp,
        retriever=retr,
        catalog=Catalog(parse_all(ROOT / "data/raw")),
        data_month="202609",
        planner="rule",
        rag_agent="fixed",
    )
    app, crashes, routes = build_graph(deps), [], {}
    try:
        bad = [
            (name, args, (await inner.call_tool(name, args))[0])
            for name, args in BAD_CALLS
        ]
        for g in golden:
            mcp.item, retr.ids, llm.route = (
                g["id"],
                g["gold_ids"],
                g["difficulty"],
            )
            try:
                prev, history = None, []
                if g.get("setup"):
                    f0, s0 = await run_v2(
                        app, deps, g["setup"], thread_id=g["id"]
                    )
                    prev = s0.get("slots")
                    history = [
                        {"role": "user", "content": g["setup"]},
                        {"role": "assistant", "content": f0["reply"]},
                    ]
                _, s = await asyncio.wait_for(
                    run_v2(
                        app,
                        deps,
                        g["query"],
                        thread_id=g["id"],
                        history=history,
                        prev_slots=prev,
                    ),
                    60,
                )
                routes[g["id"]] = s["route"]
            except Exception as e:  # noqa: BLE001 — 문항별 예외를 모아 보고하고 종료 코드 1로 실패시킨다
                crashes.append(
                    {"item": g["id"], "error": f"{type(e).__name__}: {e}"[:300]}
                )
    finally:
        await inner.stop()

    by_tool = defaultdict(list)
    for c in mcp.calls:
        by_tool[c["tool"]].append(c)
    print(
        f"골든 {len(golden)}문항, 도구 호출 {len(mcp.calls)}회, 그래프 예외 {len(crashes)}건\n"
    )
    print(
        "| 도구 | 호출 | 성공 | 실패 | 재시도 | 지연 중앙값(ms) | "
        "최대(ms) | 0건 조회 |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for tool, cs in sorted(by_tool.items()):
        ms = [c["ms"] for c in cs]
        empty = sum(1 for c in cs if c.get("rows") == 0)
        print(
            f"| {tool} | {len(cs)} | {sum(c['ok'] for c in cs)} | {sum(not c['ok'] for c in cs)} | "
            f"{sum(c['retries'] for c in cs)} | {st.median(ms):.1f} | {max(ms):.1f} | {empty if tool == 'query_products' else '-'} |"
        )
    fails = [c for c in mcp.calls if not c["ok"]]
    if fails:
        print("\n실패한 호출")
        for c in fails:
            print(f"- {c['item']} {c['tool']} {c.get('args')} → {c['error']}")
    if crashes:
        print("\n그래프 예외")
        for c in crashes:
            print(f"- {c['item']}: {c['error']}")
    print("\n잘못된 호출의 오류 문구")
    for name, args, res in bad:
        print(
            f"- {name} {args} → {'통과(!)' if res.ok else res.error.splitlines()[0][:110]}"
        )
    wrong = {
        i: r
        for i, r in routes.items()
        if r != next(g["difficulty"] for g in golden if g["id"] == i)
        and r in ("easy", "hard")
    }
    print(f"\n규칙 라우팅이 골든 difficulty와 다른 문항: {wrong or '없음'}")
    out = ROOT / "eval/results/tool_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"calls": mcp.calls, "crashes": crashes, "routes": routes},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"원본: {out.relative_to(ROOT)}")
    return 1 if fails or crashes or any(res.ok for _, _, res in bad) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
