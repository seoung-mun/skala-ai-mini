"""골든셋으로 v1(베이스라인)·v2를 같은 조건에서 반복 측정한다.

사용:
  python eval/run_eval.py baseline --repeat 3
  python eval/run_eval.py v2 --repeat 3
  python eval/run_eval.py v3 --repeat 1    # 현재 그래프 코드를 v3 결과로 저장(v2 결과는 보존)
  python eval/run_eval.py baseline --missing   # 골든셋에 새로 추가된 문항만 측정해 기존 결과에 덧붙임
  python eval/run_eval.py compare          # eval/results/compare.md 생성
  python eval/run_eval.py rescore          # 저장된 답변을 현재 채점기로 다시 채점(LLM 호출 없음)

베이스라인은 조건(골든셋·데이터·모델)을 고정한 뒤 한 번만 측정한다. 채점 규칙이 바뀌면 rescore만 한다.
"""
import argparse
import asyncio
import json
import statistics as st
import time
from collections import defaultdict
from pathlib import Path

from finagent.baseline import run_baseline
from finagent.config import load_config
from finagent.graph.build import build_graph, run_v2
from finagent.graph.nodes import collected
from finagent.llm import build_llm
from finagent.scoring import score
from finagent.search import KeywordIndex
from finagent.server import default_runtime

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "eval/results"
EXPECTED_ROUTE = {"chat": "chat", "out_of_scope": "out_of_scope"}


def expected_route(it: dict) -> str:
    return EXPECTED_ROUTE.get(it["category"], it["difficulty"])


async def run_baseline_only(golden: list[dict], repeat: int) -> list[dict]:
    """v1은 키워드 색인과 LLM만 쓴다 — 임베딩 모델·MCP 서버를 띄우지 않아 메모리를 아낀다."""
    llm, index = build_llm(load_config()), KeywordIndex.from_dir(ROOT / "data/md")
    await asyncio.to_thread(llm.chat, "warmup", "안녕")  # 모델 로딩을 측정에서 제외
    rows = []
    for rep in range(repeat):
        for it in golden:
            r = await asyncio.to_thread(run_baseline, it["query"], index, llm)
            res = {"answer": r["answer"], "retrieved": r["retrieved"], "route": "baseline",
                   "latency_s": r["latency_s"], "llm_calls": 1, "retries": 0,
                   "prompt_tokens": r["usage"]["prompt_tokens"],
                   "completion_tokens": r["usage"]["completion_tokens"], "usd": r["usage"]["usd"]}
            rows.append(_record(it, res, rep))
    return rows


def _record(it: dict, res: dict, rep: int) -> dict:
    row = {**it, **res, **score(it, res), "repeat": rep}
    print(f"[{rep}] {it['id']:4} {'O' if row['correct'] else 'X'} route={res['route']:12} "
          f"{res['latency_s']:6.1f}s llm={res['llm_calls']} | {res['answer'][:60]!r}", flush=True)
    return row


async def run_system(system: str, golden: list[dict], repeat: int) -> list[dict]:
    if system == "baseline":
        return await run_baseline_only(golden, repeat)
    rt = await default_runtime()
    app = build_graph(rt.deps)
    rows = []
    try:
        await asyncio.to_thread(rt.deps.llm.chat, "warmup", "안녕")  # 모델 로딩을 측정에서 제외
        for rep in range(repeat):
            for it in golden:
                # v2 (v1은 run_baseline_only — 대화 메모리가 없어 setup 질문을 쓰지 않는다)
                prev = None
                if it.get("setup"):
                    _, s0 = await run_v2(app, rt.deps, it["setup"], thread_id=f"{it['id']}-{rep}")
                    prev = s0.get("slots")
                t0 = time.perf_counter()
                final, s = await run_v2(app, rt.deps, it["query"], thread_id=f"{it['id']}-{rep}", prev_slots=prev)
                m = final["metadata"]
                rows_, _, doc_ids, _ = collected(s)  # 서브에이전트 결과에서 근거로 쓴 상품
                res = {"answer": final["reply"], "route": final["route"],
                       "retrieved": list(dict.fromkeys(doc_ids + [x["product_id"] for x in rows_])),
                       "latency_s": round(time.perf_counter() - t0, 2), "llm_calls": m["llm_calls"],
                       "retries": m["retries"], "validation_attempts": m["validation_attempts"],
                       "prompt_tokens": m["prompt_tokens"], "completion_tokens": m["completion_tokens"],
                       "usd": m["cost_usd"], "events": [e["type"] for e in s["events"]],
                       "verified": s.get("verified")}
                rows.append(_record(it, res, rep))
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
    names = [(n, label) for n, label in (("baseline", "v1 베이스라인"), ("v2", "v2"), ("v3", "v3"))
             if (RES / f"{n}_summary.json").exists()]
    cols = [json.loads((RES / f"{n}_summary.json").read_text()) for n, _ in names]
    pct = lambda x: f"{x * 100:.1f}%" if x is not None else "-"
    rows = [("문항 수", *(c["n_items"] for c in cols)),
            ("정답률 (전체)", *(pct(c["accuracy"]) for c in cols)),
            ("정답률 run별", *(", ".join(map(pct, c["accuracy_per_run"])) for c in cols))]
    for k in sorted(set().union(*(c["accuracy_by"] for c in cols))):
        rows.append((f"정답률 ({k})", *(pct(c["accuracy_by"].get(k)) for c in cols)))
    rows += [("검색 적중 (정답 상품이 근거에 포함)", *(pct(c["hit_at_k"]) for c in cols)),
             ("지연 중앙값 / p90 (초)", *(f"{c['latency_median_s']} / {c['latency_p90_s']}" for c in cols)),
             ("프롬프트 토큰 / 문항", *(c["prompt_tokens_per_q"] for c in cols)),
             ("생성 토큰 / 문항", *(c["completion_tokens_per_q"] for c in cols)),
             ("가상 비용 USD / 문항", *(c["usd_per_q"] for c in cols)),
             ("LLM 호출 / 문항", *(c["llm_calls_per_q"] for c in cols)),
             ("도구 retry (run당)", *(c["retries_total"] for c in cols)),
             ("라우팅 정확도", *(pct(c.get("route_accuracy")) for c in cols)),
             ("검증 시도 / 문항", *(c.get("validation_attempts_per_q", "-") for c in cols)),
             ("검증 실패 → 도구 결과 대체 (run당)", *(c.get("unverified_fallback", "-") for c in cols))]
    md = (f"# Before / After (같은 데이터·같은 모델, 각 {cols[0]['repeats']}회)\n\n"
          "문항 수가 다르면 늦게 추가된 문항(v3 역산 G1~G3)이 앞 버전 측정에 없다는 뜻이다.\n\n"
          f"| 지표 | {' | '.join(l for _, l in names)} |\n|---|{'---|' * len(names)}\n"
          + "\n".join("| " + " | ".join(map(str, r)) + " |" for r in rows) + "\n")
    (RES / "compare.md").write_text(md, encoding="utf-8")
    return md


def rescore() -> None:
    """저장된 답변을 현재 골든셋·채점기로 다시 채점한다. 질문이 바뀐 문항이 있으면 재측정이 필요하므로 중단한다."""
    golden = {g["id"]: g for g in (json.loads(l) for l in (ROOT / "eval/golden.jsonl").read_text().splitlines())}
    for system in ("baseline", "v2", "v3"):
        path = RES / f"{system}.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        changed = sorted({r["id"] for r in rows if r["id"] not in golden or golden[r["id"]]["query"] != r["query"]})
        if changed:
            raise SystemExit(f"{system}: 골든셋 질문이 바뀐 문항 {changed} — rescore가 아니라 재측정이 필요합니다")
        flips = 0
        for r in rows:
            g = golden[r["id"]]
            new = score(g, r)
            flips += new["correct"] != r["correct"]
            r.update(check=g["check"], gold_ids=g["gold_ids"], retrieval=g["retrieval"], **new)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        s = summarize(rows)
        (RES / f"{system}_summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{system}: {len(rows)}행 재채점, 판정 변경 {flips}건, 정답률 {s['accuracy']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("system", choices=["baseline", "v2", "v3", "compare", "rescore"])
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--ids", help="쉼표로 구분한 문항 id만 실행(점검용, 결과 파일은 *_pilot로 저장)")
    ap.add_argument("--missing", action="store_true", help="기존 결과에 없는 문항만 측정해 덧붙임(동결 결과는 그대로)")
    a = ap.parse_args()
    if a.system == "compare":
        print(compare())
        return
    if a.system == "rescore":
        rescore()
        return
    golden = [json.loads(l) for l in (ROOT / "eval/golden.jsonl").read_text().splitlines()]
    name = a.system
    if a.ids:
        golden, name = [g for g in golden if g["id"] in a.ids.split(",")], f"{a.system}_pilot"
    prev: list[dict] = []
    if a.missing:
        path = RES / f"{name}.jsonl"
        prev = [json.loads(l) for l in path.read_text().splitlines()]
        reps = sorted({r["repeat"] for r in prev})
        if reps != list(range(a.repeat)):
            raise SystemExit(f"{name}: 기존 결과는 repeat {reps} — 같은 --repeat로 덧붙여야 합니다")
        done = {r["id"] for r in prev}
        golden = [g for g in golden if g["id"] not in done]
        if not golden:
            raise SystemExit(f"{name}: 새로 측정할 문항이 없습니다")
    rows = prev + asyncio.run(run_system(a.system, golden, a.repeat))
    with (RES / f"{name}.jsonl").open("w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    s = summarize(rows)
    (RES / f"{name}_summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
