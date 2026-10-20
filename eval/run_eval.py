"""골든셋으로 v1(베이스라인)·v2를 같은 조건에서 반복 측정한다.

사용:
  python eval/run_eval.py baseline --repeat 3
  python eval/run_eval.py v2 --repeat 3
  python eval/run_eval.py compare          # eval/results/compare.md 생성
"""
import argparse
import asyncio
import json
import statistics as st
import time
from collections import defaultdict
from pathlib import Path

from finagent.baseline import run_baseline
from finagent.graph.build import build_graph, run_v2
from finagent.scoring import score
from finagent.server import default_runtime

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "eval/results"
EXPECTED_ROUTE = {"chat": "chat", "out_of_scope": "out_of_scope"}


def expected_route(it: dict) -> str:
    return EXPECTED_ROUTE.get(it["category"], it["difficulty"])


async def run_system(system: str, golden: list[dict], repeat: int) -> list[dict]:
    rt = await default_runtime()  # v1·v2가 같은 데이터·같은 LLM을 쓴다
    app = build_graph(rt.deps)
    rows = []
    try:
        await asyncio.to_thread(rt.deps.llm.chat, "warmup", "안녕")  # 모델 로딩을 측정에서 제외
        for rep in range(repeat):
            for it in golden:
                if system == "baseline":  # v1은 대화 메모리가 없어 setup 질문을 쓰지 않는다
                    r = await asyncio.to_thread(run_baseline, it["query"], rt.index, rt.deps.llm)
                    res = {"answer": r["answer"], "retrieved": r["retrieved"], "route": "baseline",
                           "latency_s": r["latency_s"], "llm_calls": 1, "retries": 0,
                           "prompt_tokens": r["usage"]["prompt_tokens"],
                           "completion_tokens": r["usage"]["completion_tokens"], "usd": r["usage"]["usd"]}
                else:
                    prev = None
                    if it.get("setup"):
                        _, s0 = await run_v2(app, rt.deps, it["setup"], thread_id=f"{it['id']}-{rep}")
                        prev = s0.get("slots")
                    t0 = time.perf_counter()
                    final, s = await run_v2(app, rt.deps, it["query"], thread_id=f"{it['id']}-{rep}", prev_slots=prev)
                    m = final["metadata"]
                    res = {"answer": final["reply"], "route": final["route"],
                           "retrieved": list(dict.fromkeys(list(s.get("docs", {})) +
                                                           [x["product_id"] for x in s.get("structured", [])])),
                           "latency_s": round(time.perf_counter() - t0, 2), "llm_calls": m["llm_calls"],
                           "retries": m["retries"], "validation_attempts": m["validation_attempts"],
                           "prompt_tokens": m["prompt_tokens"], "completion_tokens": m["completion_tokens"],
                           "usd": m["cost_usd"], "events": [e["type"] for e in s["events"]],
                           "verified": s.get("verified")}
                row = {**it, **res, **score(it, res), "repeat": rep}
                rows.append(row)
                print(f"[{rep}] {it['id']:4} {'O' if row['correct'] else 'X'} route={res['route']:12} "
                      f"{res['latency_s']:6.1f}s llm={res['llm_calls']} | {res['answer'][:60]!r}", flush=True)
    finally:
        if rt.close:
            await rt.close()
        rt.sessions.close()
    return rows


def summarize(rows: list[dict]) -> dict:
    reps = sorted({r["repeat"] for r in rows})
    by_rep = [[r for r in rows if r["repeat"] == k] for k in reps]
    first = by_rep[0]
    acc = lambda rs: sum(r["correct"] for r in rs) / len(rs)
    groups = defaultdict(list)
    for r in rows:
        groups[r["difficulty"]].append(r)
        groups[r["category"]].append(r)
    hits = [r["retrieval_hit"] for r in rows if r["retrieval_hit"] is not None]
    per_item_lat = defaultdict(list)
    for r in rows:
        per_item_lat[r["id"]].append(r["latency_s"])
    lat = sorted(st.median(v) for v in per_item_lat.values())
    n = len(reps)
    out = {"n_items": len(first), "repeats": n,
           "accuracy_per_run": [round(acc(rs), 3) for rs in by_rep],
           "accuracy": round(acc(rows), 3),
           "accuracy_by": {k: round(acc(v), 3) for k, v in sorted(groups.items())},
           "hit_at_k": round(sum(hits) / len(hits), 3) if hits else None,
           "latency_median_s": round(st.median(lat), 1), "latency_p90_s": round(lat[int(0.9 * (len(lat) - 1))], 1),
           "prompt_tokens_per_q": round(sum(r["prompt_tokens"] for r in rows) / len(rows)),
           "completion_tokens_per_q": round(sum(r["completion_tokens"] for r in rows) / len(rows)),
           "usd_per_q": round(sum(r["usd"] for r in rows) / len(rows), 5),
           "llm_calls_per_q": round(sum(r["llm_calls"] for r in rows) / len(rows), 2),
           "retries_total": sum(r["retries"] for r in rows) // n}
    if rows[0]["route"] != "baseline":
        out["route_accuracy"] = round(sum(r["route"] == expected_route(r) for r in rows) / len(rows), 3)
        out["validation_attempts_per_q"] = round(sum(r.get("validation_attempts", 0) for r in rows) / len(rows), 2)
        out["unverified_fallback"] = sum(r.get("verified") is False for r in rows) // n
    return out


def compare() -> str:
    a = json.loads((RES / "baseline_summary.json").read_text())
    b = json.loads((RES / "v2_summary.json").read_text())
    pct = lambda x: f"{x * 100:.1f}%" if x is not None else "-"
    rows = [("정답률 (전체)", pct(a["accuracy"]), pct(b["accuracy"])),
            ("정답률 run별", ", ".join(map(pct, a["accuracy_per_run"])), ", ".join(map(pct, b["accuracy_per_run"])))]
    for k in sorted(set(a["accuracy_by"]) | set(b["accuracy_by"])):
        rows.append((f"정답률 ({k})", pct(a["accuracy_by"].get(k)), pct(b["accuracy_by"].get(k))))
    rows += [("검색 적중 (정답 상품이 근거에 포함)", pct(a["hit_at_k"]), pct(b["hit_at_k"])),
             ("지연 중앙값 / p90 (초)", f"{a['latency_median_s']} / {a['latency_p90_s']}",
              f"{b['latency_median_s']} / {b['latency_p90_s']}"),
             ("프롬프트 토큰 / 문항", a["prompt_tokens_per_q"], b["prompt_tokens_per_q"]),
             ("생성 토큰 / 문항", a["completion_tokens_per_q"], b["completion_tokens_per_q"]),
             ("가상 비용 USD / 문항", a["usd_per_q"], b["usd_per_q"]),
             ("LLM 호출 / 문항", a["llm_calls_per_q"], b["llm_calls_per_q"]),
             ("도구 retry (run당)", a["retries_total"], b["retries_total"]),
             ("라우팅 정확도", "-", pct(b.get("route_accuracy"))),
             ("검증 시도 / 문항", "-", b.get("validation_attempts_per_q")),
             ("검증 실패 → 도구 결과 대체 (run당)", "-", b.get("unverified_fallback"))]
    md = (f"# Before / After (골든셋 {a['n_items']}문항 × {a['repeats']}회, 같은 데이터·같은 모델)\n\n"
          "| 지표 | v1 베이스라인 | v2 |\n|---|---|---|\n" + "\n".join(f"| {k} | {x} | {y} |" for k, x, y in rows) + "\n")
    (RES / "compare.md").write_text(md, encoding="utf-8")
    return md


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("system", choices=["baseline", "v2", "compare"])
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--ids", help="쉼표로 구분한 문항 id만 실행(점검용, 결과 파일은 *_pilot로 저장)")
    a = ap.parse_args()
    if a.system == "compare":
        print(compare())
        return
    golden = [json.loads(l) for l in (ROOT / "eval/golden.jsonl").read_text().splitlines()]
    name = a.system
    if a.ids:
        golden, name = [g for g in golden if g["id"] in a.ids.split(",")], f"{a.system}_pilot"
    rows = asyncio.run(run_system(a.system, golden, a.repeat))
    with (RES / f"{name}.jsonl").open("w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    s = summarize(rows)
    (RES / f"{name}_summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
