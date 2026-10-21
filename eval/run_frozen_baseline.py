"""동결 스냅샷 전용: 현재 그래프를 import/실행하지 않는 베이스라인 5회 측정."""

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import statistics as st
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-4.1-mini-2025-04-14"
PROTOCOL = ROOT / "eval/BASELINE_PROTOCOL_20261021.md"


def now():
    """현재 UTC 시각(ISO 문자열)."""
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    """값을 JSON 파일로 저장한다."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def append(path, value):
    """값을 jsonl에 한 줄 덧붙이고 디스크에 바로 기록한다."""
    with path.open("a") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def hashes(snapshot):
    """스냅샷 파일별 SHA-256(입력 변경 감지용)."""
    return {
        str(p.relative_to(snapshot)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(snapshot.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def prepare(out):
    """결과 디렉터리에 코드·데이터 스냅샷과 매니페스트를 고정한다(LLM 호출 없음)."""
    out.mkdir(parents=True, exist_ok=False)
    snap = out / "snapshot"
    for folder, pattern in [("src", "*.py"), ("data/md", "*.md")]:
        for p in sorted((ROOT / folder).rglob(pattern)):
            dest = snap / p.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
    for p in [
        ROOT / "eval/golden.jsonl",
        PROTOCOL,
        Path(__file__),
        ROOT / "uv.lock",
        ROOT / "pyproject.toml",
    ]:
        dest = snap / p.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
    golden = [
        json.loads(row_text)
        for row_text in (snap / "eval/golden.jsonl").read_text().splitlines()
    ]
    assert len(golden) == len({g["id"] for g in golden}) == 43
    save(
        out / "manifest.json",
        {
            "status": "frozen",
            "created_at": now(),
            "model": MODEL,
            "system": "baseline",
            "repeats": 5,
            "items": [g["id"] for g in golden],
            "batch_id": out.name,
            "project": "finagent",
            "snapshot_sha256": hashes(snap),
            "packages": {
                p: importlib.metadata.version(p)
                for p in [
                    "langchain-openai",
                    "langchain-core",
                    "langsmith",
                    "openai",
                    "httpx",
                ]
            },
            "prices_usd_per_million": {
                "input": 0.4,
                "cached_input": 0.1,
                "output": 1.6,
            },
        },
    )
    print("FROZEN: 43 items, 5 repeats; no LLM calls")


def summary(rows):
    """측정 행을 전체·그룹별 통계로 요약한다."""

    def stats(rs):
        """행 묶음의 정확도·오류 수·지연 분위수·토큰 통계."""
        lat = sorted(r["latency_s"] for r in rs)
        known = [r for r in rs if r["prompt_tokens"] is not None]
        p, c, cached = (
            sum(r[key] for r in known)
            for key in ["prompt_tokens", "completion_tokens", "cached_tokens"]
        )
        return {
            "n": len(rs),
            "correct": sum(r["correct"] for r in rs),
            "accuracy": sum(r["correct"] for r in rs) / len(rs),
            "errors": sum(r["status"] == "error" for r in rs),
            "median_s": st.median(lat),
            "p90_s": lat[math.floor(0.9 * (len(lat) - 1))],
            "p95_s": lat[math.floor(0.95 * (len(lat) - 1))],
            "max_s": max(lat),
            "prompt_tokens": p,
            "completion_tokens": c,
            "cached_tokens": cached,
            "usd_standard": (p * 0.4 + c * 1.6) / 1e6,
            "usd_cache_adjusted": ((p - cached) * 0.4 + cached * 0.1 + c * 1.6)
            / 1e6,
            "cost_unknown_count": len(rs) - len(known),
            "retrieval_hit_at_5": st.mean(
                r["retrieval_hit"]
                for r in rs
                if r.get("retrieval_hit") is not None
            )
            if any(r.get("retrieval_hit") is not None for r in rs)
            else None,
        }

    per_run = [
        stats([r for r in rows if r["repeat"] == k])
        for k in sorted({r["repeat"] for r in rows})
    ]
    acc = [r["accuracy"] for r in per_run]
    per_item = {
        gid: {
            "successes": sum(r["correct"] for r in rows if r["id"] == gid),
            "median_s": st.median(
                r["latency_s"] for r in rows if r["id"] == gid
            ),
        }
        for gid in dict.fromkeys(r["id"] for r in rows)
    }
    med = sorted(v["median_s"] for v in per_item.values())
    return {
        "overall": stats(rows),
        "per_run": per_run,
        "accuracy_mean": st.mean(acc),
        "accuracy_sample_sd": st.stdev(acc) if len(acc) > 1 else None,
        "per_item": per_item,
        "per_item_median_distribution": {
            "median_s": st.median(med),
            "p90_s": med[math.floor(0.9 * (len(med) - 1))],
        },
        "by_group": {
            key: {
                v: stats([r for r in rows if r[key] == v])
                for v in sorted({r[key] for r in rows})
            }
            for key in ["difficulty", "category"]
        },
    }


def execute(out):
    """해시가 일치하는 동결 스냅샷으로 베이스라인을 측정한다. 이미 결과가 있으면 다시 돌리지 않는다."""
    snap = out / "snapshot"
    manifest = json.loads((out / "manifest.json").read_text())
    assert hashes(snap) == manifest["snapshot_sha256"], "Frozen inputs changed"
    if (out / "baseline.jsonl").exists() or (out / "attempts.jsonl").exists():
        raise SystemExit(
            "Existing attempts/results preserved; do not rerun this batch."
        )
    sys.path.insert(0, str(snap / "src"))
    from langchain_core.callbacks import BaseCallbackHandler
    from langsmith import Client, trace, utils

    from finagent.baseline import run_baseline
    from finagent.config import load_config
    from finagent.llm import OpenAILLM
    from finagent.scoring import score
    from finagent.search import KeywordIndex

    class Capture(BaseCallbackHandler):
        """LLM 응답의 실제 모델명과 캐시 토큰 수를 잡는 콜백."""

        def on_llm_end(self, response, **kwargs):
            """응답 메타데이터에서 모델명·캐시 토큰 수를 기록한다."""
            message = response.generations[0][0].message
            self.model = message.response_metadata.get("model_name")
            self.cached = (
                (message.usage_metadata or {})
                .get("input_token_details", {})
                .get("cache_read", 0)
            )

    cfg = load_config(ROOT / ".env")
    assert cfg.langsmith_tracing and utils.tracing_is_enabled()
    assert os.getenv("LANGSMITH_PROJECT") == "finagent"
    assert (
        not cfg.openai_base_url
        or cfg.openai_base_url.rstrip("/") == "https://api.openai.com/v1"
    )
    llm = OpenAILLM(MODEL, cfg.require("openai_api_key", "baseline evaluation"))
    capture = Capture()
    manifest.update(
        status="running",
        started_at=now(),
        temperature=0,
        sdk_max_retries=llm.client.root_client.max_retries,
        sdk_timeout_seconds={
            k: getattr(llm.client.root_client._client.timeout, k)
            for k in ["connect", "read", "write", "pool"]
        },
    )
    llm.client = llm.client.with_config(callbacks=[capture])
    index = KeywordIndex.from_dir(snap / "data/md")
    golden = [
        json.loads(row_text)
        for row_text in (snap / "eval/golden.jsonl").read_text().splitlines()
    ]
    assert len(index.docs) == 988
    save(out / "manifest.json", manifest)
    client, rows = Client(timeout_ms=15000), []
    try:
        for rep in range(5):
            for g in golden:
                meta = {
                    "item_id": g["id"],
                    "repeat": rep + 1,
                    "system": "baseline",
                    "batch_id": out.name,
                    "model": MODEL,
                    "protocol_sha256": manifest["snapshot_sha256"][
                        "eval/BASELINE_PROTOCOL_20261021.md"
                    ],
                }
                capture.model, capture.cached = None, 0
                with trace(
                    f"baseline / 회차 {rep + 1} / {g['id']}",
                    client=client,
                    project_name="finagent",
                    metadata=meta,
                    tags=[
                        "baseline-5x",
                        f"batch:{out.name}",
                        f"item:{g['id']}",
                        f"repeat:{rep + 1}",
                    ],
                    inputs={"question": g["query"]},
                ) as run:
                    append(
                        out / "attempts.jsonl",
                        {
                            "id": g["id"],
                            "repeat_number": rep + 1,
                            "run_id": str(run.id),
                            "started_at": now(),
                        },
                    )
                    started = time.perf_counter()
                    try:
                        result = run_baseline(g["query"], index, llm)
                        elapsed = time.perf_counter() - started
                        usage = result["usage"]
                        row = {
                            **g,
                            **result,
                            **score(g, result),
                            "status": "ok",
                            "prompt_tokens": usage["prompt_tokens"],
                            "completion_tokens": usage["completion_tokens"],
                            "cached_tokens": capture.cached,
                            "response_model": capture.model,
                        }
                    except Exception as exc:
                        elapsed = time.perf_counter() - started
                        row = {
                            **g,
                            "answer": "",
                            "retrieved": [],
                            "correct": False,
                            "status": "error",
                            "error_type": type(exc).__name__,
                            "prompt_tokens": None,
                            "completion_tokens": None,
                            "cached_tokens": None,
                            "response_model": capture.model,
                        }
                    row.update(
                        latency_s=elapsed,
                        repeat=rep,
                        repeat_number=rep + 1,
                        run_id=str(run.id),
                        model=MODEL,
                    )
                    run.end(
                        outputs={
                            "answer": row["answer"],
                            "correct": row["correct"],
                            "status": row["status"],
                            "latency_s": elapsed,
                        }
                    )
                append(out / "baseline.jsonl", row)
                rows.append(row)
                print(
                    f"[{rep + 1}/5] {g['id']} {'O' if row['correct'] else 'X'} {elapsed:.2f}s status={row['status']}",
                    flush=True,
                )
        assert len(rows) == len({(r["id"], r["repeat"]) for r in rows}) == 215
        assert hashes(snap) == manifest["snapshot_sha256"], (
            "Frozen inputs changed during run"
        )
        save(out / "baseline_summary.json", summary(rows))
        manifest.update(
            status="complete", finished_at=now(), completed=len(rows)
        )
    finally:
        if manifest["status"] != "complete":
            manifest.update(
                status="interrupted", finished_at=now(), completed=len(rows)
            )
        save(out / "manifest.json", manifest)
        client.flush(timeout=30)


def verify(out):
    """LangSmith 원격 추적이 저장된 결과와 일치하는지 확인하고 검증 파일을 남긴다."""
    from langsmith import Client

    from finagent.config import load_config

    load_config()
    rows = [
        json.loads(row_text)
        for row_text in (out / "baseline.jsonl").read_text().splitlines()
    ]
    client = Client(timeout_ms=20000)
    roots = {
        str(r.id): r
        for r in client.list_runs(
            project_name="finagent",
            run_ids=[r["run_id"] for r in rows],
            is_root=True,
        )
    }
    children_by_trace = {}
    for c in client.list_runs(
        project_name="finagent",
        run_type="llm",
        filter=f'has(tags, "batch:{out.name}")',
    ):
        children_by_trace.setdefault(str(c.trace_id), []).append(c)
    verified = []
    for row in rows:
        r = roots[row["run_id"]]
        meta = (r.extra or {}).get("metadata", {})
        assert (
            r.end_time
            and not r.error
            and meta["item_id"] == row["id"]
            and meta["repeat"] == row["repeat_number"]
        )
        children = children_by_trace.get(str(r.trace_id), [])
        assert children and all(c.end_time for c in children)
        assert any(
            (c.extra or {}).get("metadata", {}).get("ls_model_name") == MODEL
            for c in children
        )
        verified.append(
            {
                "id": row["id"],
                "repeat_number": row["repeat_number"],
                "run_id": str(r.id),
                "trace_url": client.get_run_url(run=r),
                "llm_children": len(children),
                "remote_verified": True,
            }
        )
    assert len(verified) == 215
    save(
        out / "trace-verification.json",
        {"verified_at": now(), "count": len(verified), "cases": verified},
    )
    print(
        "REMOTE VERIFIED: 215 roots with item/repeat metadata and LLM children"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["prepare", "run", "verify", "self-check"]
    )
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    if args.mode == "self-check":
        fake = [
            {
                "id": "A",
                "repeat": k,
                "latency_s": k + 1,
                "correct": k % 2 == 0,
                "status": "ok",
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "cached_tokens": 20,
                "difficulty": "easy",
                "category": "fact",
            }
            for k in range(5)
        ]
        s = summary(fake)
        assert s["overall"]["correct"] == 3 and s["overall"]["p90_s"] == 4
        assert abs(s["overall"]["usd_standard"] - 0.00028) < 1e-12
        assert abs(s["overall"]["usd_cache_adjusted"] - 0.00025) < 1e-12
        fake[0].update(
            status="error",
            correct=False,
            prompt_tokens=None,
            completion_tokens=None,
            cached_tokens=None,
        )
        assert summary(fake)["overall"]["cost_unknown_count"] == 1
        assert (
            summary(fake)["overall"]["n"] == 5
            and summary(fake)["overall"]["errors"] == 1
        )
        print(
            "SELF-CHECK PASSED: accuracy, quantiles, cache costs and "
            "unknown error usage"
        )
    else:
        {"prepare": prepare, "run": execute, "verify": verify}[args.mode](
            args.out.resolve()
        )
