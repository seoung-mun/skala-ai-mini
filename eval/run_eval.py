"""골든셋으로 v1(베이스라인)·현재 그래프(v3)를 같은 조건에서 반복 측정한다.

사용:
  python eval/run_eval.py baseline --backend openai --repeat 1 --results-dir eval/results/openai/run-01
  python eval/run_eval.py v3 --backend openai --repeat 1 --results-dir eval/results/openai/run-01
  python eval/run_eval.py v3 --repeat 1    # 현재 그래프 코드를 v3 결과로 저장(v2 결과는 보존)
  python eval/run_eval.py baseline --missing --repeat 1 --results-dir eval/results/openai/run-01
  python eval/run_eval.py compare --results-dir eval/results/openai/run-01
  python eval/run_eval.py rescore --results-dir eval/results/openai/run-01  # LLM 호출 없음

베이스라인은 조건(골든셋·데이터·모델)을 고정한 뒤 한 번만 측정한다. 채점 규칙이 바뀌면 rescore만 한다.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics as st
import time
from collections import defaultdict
from datetime import datetime, timezone
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
    """문항의 기대 경로(카테고리별 예외가 없으면 difficulty)."""
    return EXPECTED_ROUTE.get(it["category"], it["difficulty"])


async def run_baseline_only(
    golden: list[dict], repeat: int, on_record=None, completed=()
) -> list[dict]:
    """v1은 키워드 색인과 LLM만 쓴다 — 임베딩 모델·MCP 서버를 띄우지 않아 메모리를 아낀다."""
    llm, index = (
        build_llm(load_config()),
        KeywordIndex.from_dir(ROOT / "data/md"),
    )
    if llm.name.startswith("ollama:"):
        await asyncio.to_thread(
            llm.chat, "warmup", "안녕"
        )  # 로컬 모델 로딩을 측정에서 제외
    rows = []
    for rep in range(repeat):
        for it in golden:
            if (it["id"], rep) in completed:
                continue
            r = await asyncio.to_thread(run_baseline, it["query"], index, llm)
            res = {
                "answer": r["answer"],
                "retrieved": r["retrieved"],
                "route": "baseline",
                "latency_s": r["latency_s"],
                "llm_calls": 1,
                "retries": 0,
                "prompt_tokens": r["usage"]["prompt_tokens"],
                "completion_tokens": r["usage"]["completion_tokens"],
                "usd": r["usage"]["usd"],
                "model": llm.name,
            }
            row = _record(it, res, rep)
            rows.append(row)
            if on_record:
                on_record(row)
    return rows


def _record(it: dict, res: dict, rep: int) -> dict:
    """결과 한 건을 채점해 행으로 만들고 진행 상황을 출력한다."""
    row = {**it, **res, **score(it, res), "repeat": rep}
    print(
        f"[{rep}] {it['id']:4} {'O' if row['correct'] else 'X'} route={res['route']:12} "
        f"{res['latency_s']:6.1f}s llm={res['llm_calls']} | {res['answer'][:60]!r}",
        flush=True,
    )
    return row


async def run_system(
    system: str, golden: list[dict], repeat: int, on_record=None, completed=()
) -> list[dict]:
    """지정 시스템(baseline 또는 그래프)으로 골든셋을 repeat회 실행해 채점 행을 돌려준다."""
    if system == "baseline":
        return await run_baseline_only(golden, repeat, on_record, completed)
    rt = await default_runtime()
    app = build_graph(rt.deps)
    rows = []
    try:
        if rt.llm_name.startswith("ollama:"):
            await asyncio.to_thread(rt.deps.llm.chat, "warmup", "안녕")
        for rep in range(repeat):
            for it in golden:
                if (it["id"], rep) in completed:
                    continue
                # v2 (v1은 run_baseline_only — 대화 메모리가 없어 setup 질문을 쓰지 않는다)
                prev, history = None, []
                if it.get(
                    "setup"
                ):  # 서버와 같이 직전 대화(질문·답)와 슬롯을 함께 넘긴다
                    f0, s0 = await run_v2(
                        app, rt.deps, it["setup"], thread_id=f"{it['id']}-{rep}"
                    )
                    prev = s0.get("slots")
                    history = [
                        {"role": "user", "content": it["setup"]},
                        {"role": "assistant", "content": f0["reply"]},
                    ]
                t0 = time.perf_counter()
                final, s = await run_v2(
                    app,
                    rt.deps,
                    it["query"],
                    thread_id=f"{it['id']}-{rep}",
                    history=history,
                    prev_slots=prev,
                )
                m = final["metadata"]
                rows_, _, doc_ids, _ = collected(
                    s
                )  # 서브에이전트 결과에서 근거로 쓴 상품
                res = {
                    "answer": final["reply"],
                    "route": final["route"],
                    "retrieved": list(
                        dict.fromkeys(
                            doc_ids + [x["product_id"] for x in rows_]
                        )
                    ),
                    "latency_s": round(time.perf_counter() - t0, 2),
                    "llm_calls": m["llm_calls"],
                    "retries": m["retries"],
                    "validation_attempts": m["validation_attempts"],
                    "prompt_tokens": m["prompt_tokens"],
                    "completion_tokens": m["completion_tokens"],
                    "usd": m["cost_usd"],
                    "events": [e["type"] for e in s["events"]],
                    # 도구·서브에이전트별 결과와 후속 질문 재작성 기록(도구 호출 성공률·재작성 적용률 집계용)
                    "tasks": [
                        {
                            k: e.get(k)
                            for k in (
                                "agent",
                                "ok",
                                "error",
                                "retries",
                                "elapsed_ms",
                            )
                        }
                        for e in s["events"]
                        if e["type"] == "TaskCompleted"
                    ],
                    "rewrite": s["events"][0].get("rewrite")
                    if s["events"]
                    else None,
                    "verified": s.get("verified"),
                    "model": rt.llm_name,
                }
                row = _record(it, res, rep)
                rows.append(row)
                if on_record:
                    on_record(row)
    finally:
        if rt.close:
            await rt.close()
        rt.sessions.close()
    return rows


def summarize(rows: list[dict]) -> dict:
    """채점 행을 반복·난이도·카테고리별 정확도와 지연·비용 통계로 요약한다."""
    reps = sorted({r["repeat"] for r in rows})
    by_rep = [[r for r in rows if r["repeat"] == k] for k in reps]
    first = by_rep[0]

    def acc(rs):
        return sum(r["correct"] for r in rs) / len(rs)

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
    out = {
        "n_items": len(first),
        "repeats": n,
        "accuracy_per_run": [round(acc(rs), 3) for rs in by_rep],
        "accuracy": round(acc(rows), 3),
        "accuracy_by": {k: round(acc(v), 3) for k, v in sorted(groups.items())},
        "hit_at_k": round(sum(hits) / len(hits), 3) if hits else None,
        "latency_median_s": round(st.median(lat), 1),
        "latency_p90_s": round(lat[int(0.9 * (len(lat) - 1))], 1),
        "latency_max_s": round(max(r["latency_s"] for r in rows), 1),
        "prompt_tokens_per_q": round(
            sum(r["prompt_tokens"] for r in rows) / len(rows)
        ),
        "completion_tokens_per_q": round(
            sum(r["completion_tokens"] for r in rows) / len(rows)
        ),
        "usd_per_q": round(sum(r["usd"] for r in rows) / len(rows), 5),
        "llm_calls_per_q": round(
            sum(r["llm_calls"] for r in rows) / len(rows), 2
        ),
        "retries_total": sum(r["retries"] for r in rows) // n,
    }
    if rows[0]["route"] != "baseline":
        out["route_accuracy"] = round(
            sum(r["route"] == expected_route(r) for r in rows) / len(rows), 3
        )
        out["validation_attempts_per_q"] = round(
            sum(r.get("validation_attempts", 0) for r in rows) / len(rows), 2
        )
        out["unverified_fallback"] = (
            sum(r.get("verified") is False for r in rows) // n
        )
    return out


def compare() -> str:
    """저장된 요약들을 비교표(마크다운)로 만든다. 미완료 측정이 있으면 중단한다."""
    names = [
        (n, label)
        for n, label in (
            ("baseline", "v1 베이스라인"),
            ("v2", "v2"),
            ("v3", "v3"),
        )
        if (RES / f"{n}_summary.json").exists()
    ]
    cols = [
        json.loads((RES / f"{n}_summary.json").read_text()) for n, _ in names
    ]
    if not cols:
        raise SystemExit(f"요약 결과가 없습니다: {RES}")
    for name, _ in names:
        run = RES / f"{name}_run.json"
        if run.exists() and json.loads(run.read_text())["status"] != "complete":
            raise SystemExit(
                f"{name}: 미완료 측정의 요약은 비교할 수 없습니다. --missing으로 완료하세요"
            )
    manifest = RES / "manifest.json"
    identity = json.loads(manifest.read_text()) if manifest.exists() else None

    def pct(x):
        return f"{x * 100:.1f}%" if x is not None else "-"

    rows = [
        ("문항 수", *(c["n_items"] for c in cols)),
        ("반복 횟수", *(c["repeats"] for c in cols)),
        ("정답률 (전체)", *(pct(c["accuracy"]) for c in cols)),
        (
            "정답률 run별",
            *(", ".join(map(pct, c["accuracy_per_run"])) for c in cols),
        ),
    ]
    for k in sorted(set().union(*(c["accuracy_by"] for c in cols))):
        rows.append(
            (f"정답률 ({k})", *(pct(c["accuracy_by"].get(k)) for c in cols))
        )
    rows += [
        (
            "검색 적중 (정답 상품이 근거에 포함)",
            *(pct(c["hit_at_k"]) for c in cols),
        ),
        (
            "지연 중앙값 / p90 (초)",
            *(f"{c['latency_median_s']} / {c['latency_p90_s']}" for c in cols),
        ),
        (
            "단일 요청 최대 지연 (초)",
            *(c.get("latency_max_s", "미집계") for c in cols),
        ),
        ("프롬프트 토큰 / 문항", *(c["prompt_tokens_per_q"] for c in cols)),
        ("생성 토큰 / 문항", *(c["completion_tokens_per_q"] for c in cols)),
        ("토큰 기준 추정 비용 USD / 문항", *(c["usd_per_q"] for c in cols)),
        ("LLM 호출 / 문항", *(c["llm_calls_per_q"] for c in cols)),
        ("도구 retry (run당)", *(c["retries_total"] for c in cols)),
        ("라우팅 정확도", *(pct(c.get("route_accuracy")) for c in cols)),
        (
            "검증 시도 / 문항",
            *(c.get("validation_attempts_per_q", "-") for c in cols),
        ),
        (
            "검증 실패 → 도구 결과 대체 (run당)",
            *(c.get("unverified_fallback", "-") for c in cols),
        ),
    ]
    md = (
        "# Before / After\n\n"
        + (
            f"모델: `{identity['backend']}:{identity['model']}`. 비용은 토큰 단가 기반 추정치이며 청구액이 아니다.\n\n"
            if identity
            else "과거 결과: 모델 실행 메타데이터가 없는 원본이다. 다른 "
            "제공자 결과와 합치지 않는다.\n\n"
        )
        + "문항 수·반복 횟수가 다르면 전체 정답률·지연을 동등 조건의 개선 "
        "효과로 단정하지 않는다.\n\n"
        f"| 지표 | {' | '.join(label for _, label in names)} |\n|---|{'---|' * len(names)}\n"
        + "\n".join("| " + " | ".join(map(str, r)) + " |" for r in rows)
        + "\n"
    )
    (RES / "compare.md").write_text(md, encoding="utf-8")
    return md


def prepare_results(cfg, directory: Path | None = None) -> Path:
    """제공자·모델·실행별 결과를 분리하고 다른 조건의 덧붙이기를 막는다. 비밀 값은 기록하지 않는다."""
    model = (
        cfg.openai_model if cfg.llm_backend == "openai" else cfg.ollama_model
    )
    slug = re.sub(r"[^a-zA-Z0-9_.-]", "_", model)
    path = directory or RES / cfg.llm_backend / slug / datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%S%fZ")
    data_hash = hashlib.sha256()
    for file in sorted((ROOT / "data/md").rglob("*.md")):
        data_hash.update(
            str(file.relative_to(ROOT)).encode() + b"\0" + file.read_bytes()
        )
    identity = {
        "backend": cfg.llm_backend,
        "model": model,
        "rerank": cfg.rerank,
        "planner": cfg.planner,
        "rag_agent": cfg.rag_agent,
        "openai_reasoning": cfg.openai_reasoning,
        "agent_model": cfg.agent_model,
        "agent_reasoning": cfg.agent_reasoning,
        "structured_output_strict": cfg.llm_backend == "openai",
        "data_sha256": data_hash.hexdigest(),
        "ollama_num_ctx": cfg.ollama_num_ctx
        if cfg.llm_backend == "ollama"
        else None,
    }
    manifest = path / "manifest.json"
    if manifest.exists():
        saved = json.loads(manifest.read_text())
        if any(saved.get(k) != v for k, v in identity.items()):
            raise SystemExit(
                "제공자·모델·데이터·검색 설정이 다른 결과 "
                "디렉터리입니다. 새 디렉터리를 사용하세요"
            )
    else:
        if path.exists() and any(path.glob("*.jsonl")):
            raise SystemExit(
                "실행 메타데이터 없는 과거 결과에는 새 측정을 덧붙일 수 "
                "없습니다"
            )
        path.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    **identity,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "cost_note": "토큰 단가 추정; warmup/setup, 캐시 할인·청구 조정 "
                    "미포함",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return path


def rescore() -> None:
    """저장된 답변을 현재 골든셋·채점기로 다시 채점한다. 질문이 바뀐 문항이 있으면 재측정이 필요하므로 중단한다."""
    golden = {
        g["id"]: g
        for g in (
            json.loads(row_text)
            for row_text in (ROOT / "eval/golden.jsonl")
            .read_text()
            .splitlines()
        )
    }
    for system in ("baseline", "v2", "v3"):
        path = RES / f"{system}.jsonl"
        if not path.exists():
            continue
        rows = [
            json.loads(row_text) for row_text in path.read_text().splitlines()
        ]
        changed = sorted(
            {
                r["id"]
                for r in rows
                if r["id"] not in golden
                or golden[r["id"]]["query"] != r["query"]
            }
        )
        if changed:
            raise SystemExit(
                f"{system}: 골든셋 질문이 바뀐 문항 {changed} — rescore가 아니라 재측정이 필요합니다"
            )
        flips = 0
        for r in rows:
            g = golden[r["id"]]
            new = score(g, r)
            flips += new["correct"] != r["correct"]
            r.update(
                check=g["check"],
                gold_ids=g["gold_ids"],
                retrieval=g["retrieval"],
                **new,
            )
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
        s = summarize(rows)
        (RES / f"{system}_summary.json").write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"{system}: {len(rows)}행 재채점, 판정 변경 {flips}건, 정답률 {s['accuracy']}"
        )


def main() -> None:
    """CLI 진입점: 측정·비교·재채점을 실행한다."""
    global RES
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "system", choices=["baseline", "v2", "v3", "compare", "rescore"]
    )
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument(
        "--ids",
        help="쉼표로 구분한 문항 id만 실행(점검용, 결과 파일은 *_pilot로 저장)",
    )
    ap.add_argument(
        "--missing",
        action="store_true",
        help="기존 결과에 없는 문항만 측정해 덧붙임(동결 결과는 그대로)",
    )
    ap.add_argument(
        "--backend",
        choices=["ollama", "openai"],
        help="LLM 제공자(환경 설정보다 우선)",
    )
    ap.add_argument("--model", help="선택한 제공자의 모델")
    ap.add_argument(
        "--results-dir",
        type=Path,
        help="동일 조건 측정의 결과 디렉터리(compare/rescore도 이 경로만 사용)",
    )
    a = ap.parse_args()
    if a.repeat < 1:
        ap.error("--repeat는 1 이상이어야 합니다")
    if a.results_dir:
        RES = a.results_dir.resolve()
    if a.system == "compare":
        print(compare())
        return
    if a.system == "rescore":
        rescore()
        return
    if a.backend:
        os.environ["LLM_BACKEND"] = a.backend
    cfg = load_config()
    if a.model:
        os.environ[
            "OPENAI_MODEL" if cfg.llm_backend == "openai" else "OLLAMA_MODEL"
        ] = a.model
        cfg = load_config()
    RES = prepare_results(cfg, RES if a.results_dir else None)
    print(f"결과 디렉터리: {RES}", flush=True)
    golden = [
        json.loads(row_text)
        for row_text in (ROOT / "eval/golden.jsonl").read_text().splitlines()
    ]
    name = a.system
    if a.ids:
        golden, name = (
            [g for g in golden if g["id"] in a.ids.split(",")],
            f"{a.system}_pilot",
        )
    if not golden:
        ap.error("선택한 문항이 없습니다")
    path = RES / f"{name}.jsonl"
    completed = set()
    prev: list[dict] = []
    if a.missing:
        if not path.exists():
            ap.error(
                "--missing에는 기존 결과 파일과 --results-dir가 필요합니다"
            )
        prev = [
            json.loads(row_text) for row_text in path.read_text().splitlines()
        ]
        reps = sorted({r["repeat"] for r in prev})
        if any(rep not in range(a.repeat) for rep in reps):
            raise SystemExit(
                f"{name}: 기존 결과는 repeat {reps} — 같은 --repeat로 덧붙여야 합니다"
            )
        lookup = {g["id"]: g for g in golden}
        for row in prev:
            g = lookup.get(row["id"])
            if (
                g is None
                or row["query"] != g["query"]
                or row["check"] != g["check"]
            ):
                ap.error(
                    "기존 문항·정답 기준이 달라졌습니다. 새 디렉터리에서 "
                    "재측정하거나 rescore를 사용하세요"
                )
            key = (row["id"], row["repeat"])
            if key in completed:
                ap.error("기존 결과에 문항·반복 중복이 있습니다")
            completed.add(key)
        if len(completed) == len(golden) * a.repeat:
            raise SystemExit(f"{name}: 새로 측정할 문항이 없습니다")
    elif path.exists():
        ap.error(
            "기존 결과는 덮어쓰지 않습니다. 새 디렉터리 또는 --missing을 "
            "사용하세요"
        )
    source_hash = hashlib.sha256()
    for source in sorted((ROOT / "src/finagent").rglob("*.py")):
        source_hash.update(
            str(source.relative_to(ROOT)).encode() + b"\0" + source.read_bytes()
        )
    run_info = {
        "system": a.system,
        "model": f"{cfg.llm_backend}:{cfg.openai_model if cfg.llm_backend == 'openai' else cfg.ollama_model}",
        "source_sha256": source_hash.hexdigest(),
        "harness_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "golden_sha256": hashlib.sha256(
            (ROOT / "eval/golden.jsonl").read_bytes()
        ).hexdigest(),
        "repeat": a.repeat,
        "ids": [g["id"] for g in golden],
        "status": "running",
    }
    run_path = RES / f"{name}_run.json"
    if a.missing and run_path.exists():
        saved = json.loads(run_path.read_text())
        if saved["source_sha256"] != run_info["source_sha256"]:
            ap.error(
                "소스가 달라진 측정은 이어 실행할 수 없습니다. 새 "
                "디렉터리를 사용하세요"
            )
    run_path.write_text(
        json.dumps(run_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with path.open("a" if a.missing else "x", encoding="utf-8") as f:

        def checkpoint(row):
            """결과 행을 바로 jsonl에 덧붙인다(중단돼도 이어서 측정할 수 있게)."""
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()

        try:
            rows = prev + asyncio.run(
                run_system(a.system, golden, a.repeat, checkpoint, completed)
            )
        except BaseException:
            run_info["status"] = "interrupted"
            run_path.write_text(
                json.dumps(run_info, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            raise
    s = summarize(rows)
    (RES / f"{name}_summary.json").write_text(
        json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    run_info["status"] = "complete"
    run_path.write_text(
        json.dumps(run_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(s, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
