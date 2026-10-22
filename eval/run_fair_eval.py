"""동결된 새 평가셋으로 동일 문맥의 baseline/current를 실제 실행한다."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-4.1-mini-2025-04-14"
PROTOCOL = "FAIR_PROTOCOL_20261021.md"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def append(path, value):
    with path.open("a") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def hashes(folder):
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def squash(text):
    return re.sub(r"\s+", "", text.replace("**", "").replace("`", "")).lower()


def quantities(answer, unit):
    pat = r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(%\s*p|퍼센트포인트|%|퍼센트|만\s*원|원)"
    values = []
    for m in re.finditer(pat, answer, flags=re.I):
        label = re.sub(r"\s+", "", m[2]).lower()
        actual = (
            "%p"
            if label in ("%p", "퍼센트포인트")
            else "%"
            if label in ("%", "퍼센트")
            else "원"
        )
        if actual == unit:
            value = float(m[1].replace(",", "")) * (
                10000 if label == "만원" else 1
            )
            # 부정한 숫자를 정답으로 세지 않는다. 일반 문장 전체의 의미 판정은 별도 수동 검토한다.
            if not re.match(r"\s*(?:가|이)?\s*아니", answer[m.end() :]):
                values.append(value)
    return values


def grade(item, answer):
    r, checks = item["rubric"], {}
    text = squash(answer)
    if r["type"] == "clarify":
        # 미래 정보가 없다는 거절에서 '내년'과 '미래'는 같은 의도다.
        text = text.replace("내후년", "미래").replace("내년", "미래")
    if r["type"] in ("number", "ranking"):
        vals = quantities(answer, r["unit"])
        checks["expected_value"] = any(
            abs(v - r["value"]) < 0.005 for v in vals
        )
        if r["type"] == "number" and r["unit"] in ("%", "%p"):
            checks["no_other_rates"] = bool(vals) and all(
                abs(v - r["value"]) < 0.005 for v in vals
            )
        if r.get("subject"):
            checks["subject"] = squash(r["subject"]) in text
        if r["type"] == "ranking":
            win = {squash(w["name"]) for w in r["winners"]}
            checks["all_tied_winners"] = all(w in text for w in win)
            positions = [
                (text.find(squash(c["name"])), squash(c["name"]))
                for c in r["candidates"]
                if squash(c["name"]) in text
            ]
            checks["winner_first"] = (
                bool(positions) and min(positions)[1] in win
            )
    for i, pattern in enumerate(r.get("terms", [])):
        checks[f"term_{i}"] = bool(re.search(pattern, text))
    if r["type"] == "conditions":
        vals = quantities(answer, "%p") + quantities(answer, "%")
        checks["condition_rates"] = all(
            any(abs(v - w) < 0.005 for v in vals) for w in r["rates"]
        )
    if r["type"] == "clarify":
        checks["withhold_rate"] = not quantities(
            answer, "%"
        ) and not quantities(answer, "%p")
        checks["clarification_or_refusal"] = bool(
            re.search(
                r"알려|필요|없|어렵|어려|불가|확인|제공|죄송|정보만안내|확정.*(?:못|없)|예측.*(?:못|없)",
                text,
            )
        )
    return {"correct": bool(checks) and all(checks.values()), "checks": checks}


def prepare(out):
    out.mkdir(parents=True, exist_ok=False)
    snap = out / "snapshot"
    for folder, pattern in [
        ("src", "*.py"),
        ("data/raw", "*.json"),
        ("data/md", "*.md"),
    ]:
        for p in sorted((ROOT / folder).rglob(pattern)):
            dest = snap / p.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
    for rel in [
        "eval/fair_suite_20261021.jsonl",
        "eval/build_fair_suite.py",
        "eval/run_fair_eval.py",
        f"eval/{PROTOCOL}",
        "uv.lock",
        "pyproject.toml",
    ]:
        dest = snap / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dest)
    items = [
        json.loads(s)
        for s in (snap / "eval/fair_suite_20261021.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(items) == len({i["id"] for i in items}) == 40
    old = {
        json.loads(s)["query"]
        for s in (ROOT / "eval/golden.jsonl").read_text().splitlines()
    }
    overlaps = [i["id"] for i in items if i["query"] in old]
    save(
        out / "manifest.json",
        {
            "status": "frozen",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": MODEL,
            "repeats": 3,
            "n_items": 40,
            "original_root": str(ROOT),
            "snapshot_sha256": hashes(snap),
            "packages": {
                p: importlib.metadata.version(p)
                for p in [
                    "openai",
                    "langchain",
                    "langchain-openai",
                    "langgraph",
                    "fastmcp",
                    "mcp",
                ]
            },
            "old_exact_query_overlap": overlaps,
            "context_policy": "same fixed user+assistant history; baseline sees serialized history; current gets history and prev_slots deterministically extracted ONLY from the provided previous user turn",
            "price_usd_per_million": {"input": 0.4, "output": 1.6},
            "order": "baseline 3 repeats then current 3 repeats; each repeat Random(20261021+repeat) shuffle",
            "execution_boundary": "actual OpenAI and MCP subprocess, graph runtime directly; no HTTP/browser",
        },
    )
    print(f"Frozen {len(items)} cases; exact old overlaps={overlaps}")


async def run(out, system):
    manifest = json.loads((out / "manifest.json").read_text())
    snap = out / "snapshot"
    assert hashes(snap) == manifest["snapshot_sha256"], "Frozen files changed"
    assert ROOT == snap, "Run the snapshot copy of this script"
    path = out / f"{system}.jsonl"
    completed = {}
    if path.exists():
        completed = {
            (r["id"], r["repeat"]): r
            for r in map(json.loads, path.read_text().splitlines())
        }
    os.environ.update(
        LLM_BACKEND="openai",
        OPENAI_MODEL=MODEL,
        AGENT_MODEL=MODEL,
        OPENAI_REASONING="",
        AGENT_REASONING="",
        PLANNER="llm",
        RAG_AGENT="react",
        RERANK="1",
    )
    sys.path.insert(0, str(snap / "src"))
    from finagent import llm as lm
    from finagent.config import load_config

    lm.OPENAI_PRICES[MODEL] = lm.OPENAI_PRICES["gpt-4.1-mini"]
    cfg = load_config(Path(manifest["original_root"]) / ".env")
    assert (
        not cfg.openai_base_url
        or cfg.openai_base_url.rstrip("/") == "https://api.openai.com/v1"
    )
    # 응답 모델·사용량을 모든 호출에서 수집하되 인증 정보는 기록하지 않는다.
    from langchain_core.callbacks import BaseCallbackHandler

    class Observed(BaseCallbackHandler):
        def __init__(self):
            self.calls = []

        def on_llm_end(self, response, **kwargs):
            message = response.generations[0][0].message
            meta = message.response_metadata
            u = message.usage_metadata
            self.calls.append(
                {
                    "model": meta.get("model_name"),
                    "usage": {
                        "prompt_tokens": u["input_tokens"],
                        "completion_tokens": u["output_tokens"],
                        "prompt_tokens_details": {
                            "cached_tokens": u.get(
                                "input_token_details", {}
                            ).get("cache_read", 0)
                        },
                    }
                    if u
                    else None,
                }
            )

    observer = Observed()
    llm = lm.build_llm(cfg)
    llm.client.callbacks = [observer]
    from finagent.search import KeywordIndex

    index = KeywordIndex.from_dir(snap / "data/md")
    from finagent.baseline import run_baseline

    mcp = None
    if system == "current":
        from legacy_rules import extract

        from finagent.catalog import Catalog
        from finagent.deps import Deps
        from finagent.graph.build import build_graph, run_v2
        from finagent.mcp_client import MCPClient
        from finagent.parser import parse_all
        from finagent.retrieval import (
            CrossEncoderReranker,
            HybridRetriever,
            KureEmbedder,
        )

        catalog = Catalog(parse_all(snap / "data/raw"))
        mcp = MCPClient()
        # MCP 자식 프로세스도 동일한 동결 소스를 사용한다.
        os.environ["PYTHONPATH"] = str(snap / "src")
        await mcp.start()
        retriever = HybridRetriever(
            index,
            KureEmbedder(),
            Path(manifest["original_root"]) / "data/emb_cache",
            reranker=CrossEncoderReranker(),
        )
        deps = Deps(
            llm=llm,
            mcp=mcp,
            retriever=retriever,
            catalog=catalog,
            data_month=max(p.base["dcls_month"] for p in catalog.products),
            planner="llm",
            rag_agent="react",
            agent_llm=llm,
        )
        app = build_graph(deps)
    items = [
        json.loads(s)
        for s in (snap / "eval/fair_suite_20261021.jsonl")
        .read_text()
        .splitlines()
    ]
    try:
        for rep in range(3):
            order = list(items)
            random.Random(20261021 + rep).shuffle(order)
            for item in order:
                key = (item["id"], rep)
                if key in completed:
                    continue
                attempt = {
                    "system": system,
                    "id": item["id"],
                    "repeat": rep,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
                append(out / "attempts.jsonl", attempt)
                observer.calls = []
                t0 = time.perf_counter()
                row = {
                    **attempt,
                    "category": item["category"],
                    "query": item["query"],
                    "history": item["history"],
                    "status": "ok",
                    "verified": None,
                    "tasks": [],
                    "validation": [],
                    "warnings": [],
                }
                try:
                    if system == "baseline":
                        query = item["query"]
                        if item["history"]:
                            query = (
                                "## 이전 대화\n"
                                + "\n".join(
                                    f"{m['role']}: {m['content']}"
                                    for m in item["history"]
                                )
                                + "\n## 현재 질문\n"
                                + query
                            )
                        r = await asyncio.to_thread(
                            run_baseline, query, index, llm
                        )
                        row.update(answer=r["answer"], retrieved=r["retrieved"])
                    else:
                        final, state = await run_v2(
                            app,
                            deps,
                            item["query"],
                            thread_id=f"fair-{item['id']}-{rep}",
                            history=item["history"],
                            prev_slots=extract(
                                item["history"][-2]["content"], catalog
                            ).to_dict()
                            if item["history"]
                            else None,
                        )
                        row.update(
                            answer=final["reply"],
                            verified=state.get("verified"),
                            tasks=[
                                e
                                for e in state["events"]
                                if e["type"] == "TaskCompleted"
                            ],
                            validation=[
                                e
                                for e in state["events"]
                                if e["type"] == "Validation"
                            ],
                            warnings=final.get("warnings", []),
                            rewrite=next(
                                (
                                    e.get("rewrite")
                                    for e in state["events"]
                                    if e.get("rewrite")
                                ),
                                None,
                            ),
                            final=final,
                        )
                    row.update(grade(item, row["answer"]))
                except Exception as e:
                    row.update(
                        status="error",
                        error_type=type(e).__name__,
                        answer="",
                        correct=False,
                        checks={"execution": False},
                    )
                row["latency_s"] = time.perf_counter() - t0
                row["observed_calls"] = observer.calls
                usages = [c["usage"] for c in observer.calls if c["usage"]]
                row["llm_calls"] = len(observer.calls)
                row["prompt_tokens"] = sum(u["prompt_tokens"] for u in usages)
                row["completion_tokens"] = sum(
                    u["completion_tokens"] for u in usages
                )
                row["cached_tokens"] = sum(
                    (u.get("prompt_tokens_details") or {}).get(
                        "cached_tokens", 0
                    )
                    for u in usages
                )
                row["usage_incomplete"] = row["status"] == "error" or len(
                    usages
                ) != len(observer.calls)
                row["usd_standard"] = (
                    row["prompt_tokens"] * 0.4 + row["completion_tokens"] * 1.6
                ) / 1e6
                row["models_ok"] = all(
                    c["model"] == MODEL for c in observer.calls
                ) and bool(observer.calls)
                append(path, row)
                print(
                    f"{system} {rep + 1}/3 {item['id']} {'PASS' if row['correct'] else 'FAIL'} {row['latency_s']:.1f}s calls={row['llm_calls']}",
                    flush=True,
                )
                assert hashes(snap) == manifest["snapshot_sha256"], (
                    "Frozen files changed during run"
                )
    finally:
        if mcp:
            await mcp.stop()
    print(f"{system} complete", flush=True)


def stats(rows):
    lat = sorted(r["latency_s"] for r in rows)
    return {
        "n": len(rows),
        "correct": sum(r["correct"] for r in rows),
        "accuracy": statistics.mean(r["correct"] for r in rows),
        "errors": sum(r["status"] == "error" for r in rows),
        "latency_median_s": statistics.median(lat),
        "latency_p90_s": lat[math.floor(0.9 * (len(lat) - 1))],
        "latency_max_s": max(lat),
        "prompt_tokens_mean": statistics.mean(r["prompt_tokens"] for r in rows),
        "completion_tokens_mean": statistics.mean(
            r["completion_tokens"] for r in rows
        ),
        "llm_calls_mean": statistics.mean(r["llm_calls"] for r in rows),
        "usd_standard": sum(r["usd_standard"] for r in rows),
        "unknown_usage": sum(r["usage_incomplete"] for r in rows),
        "unexpected_model_rows": sum(not r["models_ok"] for r in rows),
    }


def report(out, audited=False):
    suffix = "_audited" if audited else ""
    all_rows = {
        s: [
            json.loads(x)
            for x in (out / f"{s}{suffix}.jsonl").read_text().splitlines()
        ]
        for s in ["baseline", "current"]
    }
    result = {
        s: {
            "overall": stats(rs),
            "by_category": {
                c: stats([r for r in rs if r["category"] == c])
                for c in sorted({r["category"] for r in rs})
            },
            "per_repeat": {
                str(k + 1): stats([r for r in rs if r["repeat"] == k])
                for k in range(3)
            },
            "all_3_correct": sum(
                all(r["correct"] for r in rs if r["id"] == i)
                for i in {r["id"] for r in rs}
            ),
        }
        for s, rs in all_rows.items()
    }
    keys = [{(r["id"], r["repeat"]) for r in rs} for rs in all_rows.values()]
    assert keys[0] == keys[1] and len(keys[0]) == 120
    assert all(len(rs) == 120 for rs in all_rows.values())
    save(out / f"summary{suffix}.json", result)
    items = [
        json.loads(s)
        for s in (out / "snapshot/eval/fair_suite_20261021.jsonl")
        .read_text()
        .splitlines()
    ]
    by = {
        s: {(r["id"], r["repeat"]): r for r in rs} for s, rs in all_rows.items()
    }
    pairs = [
        (by["baseline"][k]["correct"], by["current"][k]["correct"])
        for k in sorted(keys[0])
    ]
    save(
        out / f"integrity{suffix}.json",
        {
            "snapshot_unchanged": hashes(out / "snapshot")
            == json.loads((out / "manifest.json").read_text())[
                "snapshot_sha256"
            ],
            "unique_pairs": 120,
            "rows": {s: len(rs) for s, rs in all_rows.items()},
            "improved_observations": sum(not a and b for a, b in pairs),
            "regressed_observations": sum(a and not b for a, b in pairs),
            "note": "Repeated questions are dependent; no independent 120-question significance claim.",
        },
    )
    lines = [
        "# 새 골든셋 동일 조건 Before / After 실측",
        "",
        "40문항 × 3회 × 두 시스템. 정답·채점기를 실행 전에 동결하고, 실제 OpenAI와 최신 버전의 실제 MCP subprocess를 호출했다. 과거 결과를 재사용하지 않았다.",
        "",
        "| 지표 | 키워드 top-5 RAG | 최신 감독 그래프 |",
        "|---|---:|---:|",
    ]
    for key, label in [
        ("accuracy", "규칙 채점 통과율"),
        ("correct", "정답 관측 수 / 120"),
        ("latency_median_s", "지연 중앙값(초)"),
        ("latency_p90_s", "지연 p90(초)"),
        ("latency_max_s", "최대 지연(초)"),
        ("prompt_tokens_mean", "문항당 누적 입력 토큰"),
        ("completion_tokens_mean", "문항당 누적 출력 토큰"),
        ("llm_calls_mean", "문항당 모델 호출 수"),
        ("usd_standard", "전체 일반 단가 추정 USD"),
        ("errors", "실행 오류"),
        ("unknown_usage", "사용량 불완전 관측"),
    ]:
        values = [result[s]["overall"][key] for s in ["baseline", "current"]]

        def fmt(v):
            if key == "accuracy":
                return f"{v * 100:.1f}%"
            return f"{v:.4f}" if isinstance(v, float) else str(v)

        lines.append(f"| {label} | {fmt(values[0])} | {fmt(values[1])} |")
    lines += [
        "",
        "## 범주별 정답 관측",
        "",
        "| 범주 | baseline | current |",
        "|---|---:|---:|",
    ]
    for c in result["baseline"]["by_category"]:
        v = [result[s]["by_category"][c] for s in ["baseline", "current"]]
        lines.append(
            f"| {c} | {v[0]['correct']}/{v[0]['n']} | {v[1]['correct']}/{v[1]['n']} |"
        )
    lines += [
        "",
        "## 문항별 반복 성공·실패",
        "",
        "| ID | baseline / 3 | current / 3 | 질문 |",
        "|---|---:|---:|---|",
    ]
    for it in items:
        n = [
            sum(r["correct"] for r in all_rows[s] if r["id"] == it["id"])
            for s in ["baseline", "current"]
        ]
        lines.append(f"| {it['id']} | {n[0]} | {n[1]} | {it['query']} |")
    lines += [
        "",
        "## 해석의 한계",
        "",
        "- 이번 평가용으로 새로 작성한 진단셋이다. 기존 실패 유형을 참고했으므로 독립적인 블라인드 홀드아웃이라고 부르지 않는다. 결과를 보고 서비스 코드를 튜닝하지 않았다.",
        "- 동일한 고정 대화 이력을 양쪽에 제공했다. baseline은 대화를 질문 문자열에 직렬화하고 검색하며 current는 실제 history 인터페이스와 이전 사용자 발화에서만 재구성한 prev_slots를 사용한다. 정답·assistant 응답에서 슬롯을 주입하지 않는다. 별도 생성 setup 비용은 없다.",
        "- 단위·정답값·상품명·동률·명시 조건을 확인하는 규칙 채점이다. 모든 자연어의 금융 의미·산술·출처를 증명하지 않는다. 오답과 검증 통과 오답은 수동 검토 대상으로 남긴다.",
        "- 3회 반복은 40개 질문의 실행 변동이다. 서로 다른 120문항이나 일반 사용자 정확도로 해석하지 않는다.",
        "- baseline을 먼저 3회, current를 이후 3회 실행했다. 시간대·네트워크·프롬프트 캐시의 영향은 남는다. 초기 색인/모델 로딩, HTTP/SSE·브라우저 지연은 제외했다.",
        "- 가격은 입력 $0.40/M·출력 $1.60/M 일반 단가 추정이다. 캐시 토큰은 원본에 별도 기록하며 실제 청구액이 아니다. 실패에서 회수하지 못한 토큰은 비용에 포함하지 못한다.",
        "- 최신 구현은 평가 스냅샷 기준이다. 이후 작업 트리 변경과 동일하다고 가정하지 않는다. 성능 변화의 원인을 구성요소별로 분리한 ablation은 아니다.",
        "",
        "근거: [동결 규약](snapshot/eval/FAIR_PROTOCOL_20261021.md), [문항과 raw 근거](snapshot/eval/fair_suite_20261021.jsonl), [집계](summary.json), [무결성](integrity.json), [baseline 원본](baseline.jsonl), [current 원본](current.jsonl).",
    ]
    if audited:
        lines[-1] = (
            lines[-1]
            .replace("(summary.json)", "(summary_audited.json)")
            .replace("(integrity.json)", "(integrity_audited.json)")
        )
        lines.insert(
            2,
            "**사후 평가기 보정 참고 결과.** 질문·정답·원본 응답·제품 코드는 변경하지 않았다. 숫자 파싱과 거절 표현 인식만 보정했다. 보정 전 결과는 report.md, 변경 관측은 scorer_revision.json에 보존했다.\n",
        )
    (out / f"report{suffix}.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "action", choices=["prepare", "baseline", "current", "report"]
    )
    p.add_argument("out", type=Path)
    a = p.parse_args()
    out = a.out.resolve()
    if a.action == "prepare":
        prepare(out)
    elif a.action == "report":
        report(out)
    else:
        asyncio.run(run(out, a.action))
