"""원본 보존 → 반례 calibration → 익명 이중 judge → 저장 답변 재채점."""

import argparse
import hashlib
import json
import os
import random
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from hybrid_grade import SYSTEM, Judgment, aggregate, evaluate, judgment_schema
from hybrid_reference import build_references

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eval/results/openai/gpt-4.1-mini-2025-04-14/20261022-validator"
MODEL = "gpt-5-mini-2025-08-07"
SETTINGS = {
    "model": MODEL,
    "reasoning_effort": "medium",
    "max_completion_tokens": 8192,
}
FILES = [
    "hybrid_grade.py",
    "hybrid_reference.py",
    "rescore_hybrid.py",
    "hybrid_calibration.json",
    "hybrid_report.py",
]


def read(path):
    return json.loads(path.read_text())


def rows(path):
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def write(path, value):
    with path.open("x") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def hashes(folder):
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def code_hashes():
    return {
        name: hashlib.sha256((ROOT / "eval" / name).read_bytes()).hexdigest()
        for name in FILES
    }


def payload(item, answer, reference):
    return {
        "question": item["query"],
        "history": item["history"],
        "answer": answer,
        "reference": reference,
    }


def input_path(source, system):
    """기존 감사 파일이 있을 때만 사용하며 원본 폴더에 파일을 만들지 않는다."""
    audited = source / f"{system}_audited.jsonl"
    return audited if audited.exists() else source / f"{system}.jsonl"


def initialize(source, out):
    if source == out or source in out.parents:
        raise ValueError("출력 폴더는 원본 결과 폴더 밖이어야 합니다")
    references = build_references(source)
    items = rows(source / "snapshot/eval/fair_suite_20261021.jsonl")
    original = {s: rows(source / f"{s}.jsonl") for s in ("baseline", "current")}
    audited = {s: rows(input_path(source, s)) for s in original}
    expected = {(i["id"], rep) for i in items for rep in range(3)}
    for system in original:
        for rs in [original[system], audited[system]]:
            if (
                len(rs) != 120
                or {(r["id"], r["repeat"]) for r in rs} != expected
            ):
                raise ValueError("40문항 × 3회 입력이 완전하지 않습니다")
        before = {(r["id"], r["repeat"]): r for r in original[system]}
        for row in audited[system]:
            old = before[row["id"], row["repeat"]]
            answer = old["answer"]
            if old.get("error_type") == "ValueError" and old.get(
                "final", {}
            ).get("reply"):
                answer = old["final"]["reply"]
            if row["answer"] != answer:
                raise ValueError("기존 audit에서 답변이 변했습니다")
    out.mkdir(parents=True, exist_ok=False)
    (out / "evaluator").mkdir()
    for name in FILES:
        shutil.copyfile(ROOT / "eval" / name, out / "evaluator" / name)
    write(out / "references.json", references)
    write(out / "suite.json", items)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "input_files": {s: input_path(source, s).name for s in original},
        "source_sha256": hashes(source),
        "code_sha256": code_hashes(),
        "references_sha256": digest(references),
        "suite_sha256": digest(items),
        "prompt": SYSTEM,
        "prompt_sha256": digest(SYSTEM),
        "settings": SETTINGS,
        "schema": Judgment.model_json_schema(),
        "repetitions": 2,
        "seed": 20261021,
        "workers": 8,
        "max_attempts": 2,
        "retry_policy": "Only connection/timeout/rate-limit/5xx/schema errors; never retry a valid verdict.",
        "pricing": {
            "input_per_million": 0.25,
            "cached_input_per_million": 0.025,
            "output_per_million": 2.0,
            "source": "https://developers.openai.com/api/docs/models/gpt-5-mini",
        },
        "rules": [
            "Exact Decimal values; no numerical tolerance; declared source rounding only.",
            "Meaning extraction is model-dependent; code validates quotes/units/values.",
            "Two fixed independent judgments; disagreement is review, never majority selection.",
            "Exact identical full payloads cached; denominator remains 120 per system.",
            "Only question/history/answer/reference shown; system/internal fields/old grade hidden.",
            "Core correctness only: numbers, calculations, subject/option binding, material conditions and requested outcomes. Wording, order and unrequested background omissions are not failures.",
            "Individual bonus-rate questions need the requested rate; added conditions must be correct. Full-condition questions require material applicability terms; coupon mention is optional.",
            "Explicit arithmetic relations are extracted with source quotes and recomputed with Decimal. Extraction failure is review, not a proven answer error.",
            "Required numerical failure cannot be overturned by semantic pass.",
            "Any valid conclusive numeric or semantic failure makes a run fail; review applies when neither component conclusively fails and at least one is unclear.",
            "Evaluation results are post-hoc, not new system executions or a new held-out benchmark.",
        ],
    }
    write(out / "manifest.json", manifest)
    print(f"Prepared {out}: 40 questions / 240 preserved answers", flush=True)


def verify(out):
    manifest = read(out / "manifest.json")
    if manifest["code_sha256"] != code_hashes() or manifest[
        "prompt_sha256"
    ] != digest(SYSTEM):
        raise ValueError(
            "평가기 변경 감지: 새 출력 폴더로 전체 calibration부터 실행하세요"
        )
    if manifest["references_sha256"] != digest(read(out / "references.json")):
        raise ValueError("동결 reference 변경 감지")
    if manifest["suite_sha256"] != digest(read(out / "suite.json")):
        raise ValueError("동결 질문 변경 감지")
    if hashes(out / "evaluator") != manifest["code_sha256"]:
        raise ValueError("동결 평가기 복사본 변경 감지")
    if (
        manifest["settings"] != SETTINGS
        or manifest["schema"] != Judgment.model_json_schema()
    ):
        raise ValueError("동결 judge 설정 변경 감지")
    if manifest["source_sha256"] != hashes(Path(manifest["source"])):
        raise ValueError("원본 결과 변경 감지")
    return manifest


def make_client():
    # 기존 애플리케이션 설정 로더로 인증하며 비밀 값은 결과에 기록하지 않는다.
    os.environ["LANGSMITH_TRACING"] = "false"
    from openai import OpenAI

    from finagent.config import load_config

    cfg = load_config()
    return OpenAI(
        api_key=cfg.require("openai_api_key", "저장 답변 재채점"),
        base_url=cfg.openai_base_url or "https://api.openai.com/v1",
        timeout=120,
        max_retries=0,
    )


def judge_once(client, out, phase, key, repetition, data):
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        RateLimitError,
    )
    from pydantic import ValidationError

    folder = out / "judge" / phase
    folder.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        path = folder / f"{key}-r{repetition}-a{attempt}.json"
        if path.exists():
            record = read(path)
        else:
            started = time.monotonic()
            record = {
                "key": key,
                "repetition": repetition,
                "attempt": attempt,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                response = client.chat.completions.create(
                    **SETTINGS,
                    messages=[
                        {"role": "system", "content": SYSTEM},
                        {
                            "role": "user",
                            "content": json.dumps(data, ensure_ascii=False),
                        },
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "HybridJudgment",
                            "strict": True,
                            "schema": judgment_schema(data["reference"]),
                        },
                    },
                )
                record["raw_response"] = response.model_dump(mode="json")
                if response.model != MODEL:
                    record["error"] = "UnexpectedModel"
                else:
                    try:
                        Judgment.model_validate_json(
                            response.choices[0].message.content or ""
                        )
                    except ValidationError:
                        record["error"] = "SchemaError"
                        record["retryable"] = True
            except (APIConnectionError, APITimeoutError, RateLimitError) as e:
                record.update(error=type(e).__name__, retryable=True)
            except APIStatusError as e:
                record.update(
                    error=type(e).__name__,
                    status_code=e.status_code,
                    retryable=e.status_code >= 500,
                )
            record["elapsed_s"] = round(time.monotonic() - started, 3)
            write(path, record)
        if "error" not in record:
            raw = record["raw_response"]
            judgment = Judgment.model_validate_json(
                raw["choices"][0]["message"]["content"]
            )
            return evaluate(data, judgment)
        if not record.get("retryable"):
            break
        if attempt == 0:
            time.sleep(1)
    return {
        "numeric": "review",
        "arithmetic": "review",
        "semantic": "review",
        "verdict": "review",
        "numeric_checks": [],
        "arithmetic_checks": [],
        "semantic_checks": [],
        "semantic_schema_valid": False,
        "error": record["error"],
    }


def run_jobs(out, phase, inputs):
    manifest = verify(out)
    inputs_path = out / f"{phase}_inputs.json"
    if inputs_path.exists():
        if read(inputs_path) != inputs:
            raise ValueError("동결 judge 입력 변경 감지")
    else:
        write(inputs_path, inputs)
    jobs = [(key, rep) for key in sorted(inputs) for rep in range(2)]
    random.Random(manifest["seed"]).shuffle(jobs)
    results = {key: [None, None] for key in inputs}
    client = make_client()
    with client, ThreadPoolExecutor(max_workers=manifest["workers"]) as pool:
        # 전역 연결/모델 오류를 전체 작업 수만큼 반복하지 않는다.
        first_key, first_rep = jobs[0]
        first = judge_once(
            client, out, phase, first_key, first_rep, inputs[first_key]
        )
        if first.get("error"):
            raise RuntimeError(f"Judge 사전 호출 실패: {first['error']}")
        results[first_key][first_rep] = first
        print(f"{phase} 1/{len(jobs)} preflight {first['verdict']}", flush=True)
        pending = {
            pool.submit(
                judge_once, client, out, phase, key, rep, inputs[key]
            ): (key, rep)
            for key, rep in jobs[1:]
        }
        for done, future in enumerate(as_completed(pending), 2):
            key, rep = pending[future]
            results[key][rep] = future.result()
            print(
                f"{phase} {done}/{len(jobs)} {key[:10]} r{rep + 1} {results[key][rep]['verdict']}",
                flush=True,
            )
    verify(out)
    return results


def calibration(out):
    if (out / "calibration.json").exists():
        raise ValueError("기존 calibration 결과를 덮어쓰지 않습니다")
    verify(out)
    refs = read(out / "references.json")
    items = {i["id"]: i for i in read(out / "suite.json")}
    examples = read(out / "evaluator/hybrid_calibration.json")
    if not examples or {c["expected"] for c in examples} != {"pass", "fail"}:
        raise ValueError("정답·오답 calibration 사례가 모두 필요합니다")
    inputs, keys = {}, {}
    for case in examples:
        data = payload(items[case["qid"]], case["answer"], refs[case["qid"]])
        key = digest(
            {
                "payload": data,
                "settings": SETTINGS,
                "prompt": SYSTEM,
                "schema": Judgment.model_json_schema(),
            }
        )
        inputs[key], keys[case["id"]] = data, key
    results = run_jobs(out, "calibration", inputs)
    cases = []
    for case in examples:
        evaluations = results[keys[case["id"]]]
        combined = aggregate(evaluations)
        cases.append(
            {
                **case,
                "input_key": keys[case["id"]],
                **combined,
                "matched": combined["verdict"] == case["expected"],
                "judge_runs": evaluations,
            }
        )
    passed = all(c["matched"] and not c["verdict_disagreement"] for c in cases)
    write(
        out / "calibration.json",
        {
            "passed": passed,
            "matched": sum(c["matched"] for c in cases),
            "total": len(cases),
            "cases": cases,
        },
    )
    print(
        f"Calibration {sum(c['matched'] for c in cases)}/{len(cases)}; production allowed={passed}",
        flush=True,
    )


def calibration_gate(out):
    """요약의 passed 플래그만 믿지 않고 원본 judge 응답에서 통과를 재확인한다."""
    summary = read(out / "calibration.json")
    examples = read(out / "evaluator/hybrid_calibration.json")
    cases = summary.get("cases", [])
    if not examples or len(cases) != len(examples) or not summary.get("passed"):
        raise ValueError("calibration 실패 또는 결과 누락")
    inputs = read(out / "calibration_inputs.json")
    refs = read(out / "references.json")
    items = {i["id"]: i for i in read(out / "suite.json")}
    for example, case in zip(examples, cases, strict=True):
        data = payload(
            items[example["qid"]], example["answer"], refs[example["qid"]]
        )
        key = digest(
            {
                "payload": data,
                "settings": SETTINGS,
                "prompt": SYSTEM,
                "schema": Judgment.model_json_schema(),
            }
        )
        if (
            case["id"] != example["id"]
            or case["input_key"] != key
            or inputs.get(key) != data
        ):
            raise ValueError("calibration 입력 또는 사례 변경 감지")
        evaluated = []
        for repetition in range(2):
            paths = sorted(
                (out / "judge/calibration").glob(f"{key}-r{repetition}-a*.json")
            )
            records = [read(p) for p in paths]
            successful = [r for r in records if "error" not in r]
            if len(successful) != 1 or len(records) > 2:
                raise ValueError("calibration 실제 judge 응답 누락")
            raw = successful[0]["raw_response"]
            if raw["model"] != MODEL:
                raise ValueError("calibration 모델 불일치")
            judgment = Judgment.model_validate_json(
                raw["choices"][0]["message"]["content"]
            )
            evaluated.append(evaluate(data, judgment))
        if evaluated != case["judge_runs"] or any(
            e["verdict"] != example["expected"] for e in evaluated
        ):
            raise ValueError("calibration 원본 재검산 실패")


def rescore(out):
    manifest = verify(out)
    calibration_gate(out)
    if any(
        (out / f"{s}_hybrid.jsonl").exists() for s in ("baseline", "current")
    ):
        raise ValueError("기존 재채점 결과를 덮어쓰지 않습니다")
    source = Path(manifest["source"])
    refs = read(out / "references.json")
    items = {i["id"]: i for i in read(out / "suite.json")}
    inputs, records = {}, []
    for system in ("baseline", "current"):
        original = {
            (r["id"], r["repeat"]): r for r in rows(source / f"{system}.jsonl")
        }
        for row in rows(
            source
            / manifest.get("input_files", {}).get(
                system, f"{system}_audited.jsonl"
            )
        ):
            item = items[row["id"]]
            data = payload(item, row["answer"], refs[row["id"]])
            key = digest(
                {
                    "payload": data,
                    "settings": SETTINGS,
                    "prompt": SYSTEM,
                    "schema": Judgment.model_json_schema(),
                }
            )
            inputs[key] = data
            records.append(
                {
                    "system": system,
                    "id": row["id"],
                    "repeat": row["repeat"],
                    "category": row["category"],
                    "answer": row["answer"],
                    "question": item["query"],
                    "history": item["history"],
                    "input_key": key,
                    "old_original_correct": original[row["id"], row["repeat"]][
                        "correct"
                    ],
                    "old_audited_correct": row["correct"],
                }
            )
    print(
        f"Production {len(records)} rows / {len(inputs)} unique inputs / {len(inputs) * 2} calls",
        flush=True,
    )
    results = run_jobs(out, "production", inputs)
    for system in ("baseline", "current"):
        with (out / f"{system}_hybrid.jsonl").open("x") as f:
            for row in records:
                if row["system"] != system:
                    continue
                evaluations = results[row["input_key"]]
                result = {
                    **row,
                    **aggregate(evaluations),
                    "judge_runs": evaluations,
                }
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    verify(out)
    write(
        out / "integrity.json",
        {
            "originals_unchanged": True,
            "evaluator_unchanged": True,
            "references_unchanged": True,
            "source_sha256": hashes(source),
            "rows": len(records),
            "unique_inputs": len(inputs),
            "no_answer_generation": True,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=["prepare", "calibrate", "rescore", "report"]
    )
    parser.add_argument("out", type=Path)
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()
    out = args.out.resolve()
    if args.phase == "prepare":
        initialize(args.source.resolve(), out)
    elif args.phase == "calibrate":
        calibration(out)
    elif args.phase == "rescore":
        rescore(out)
    else:
        from hybrid_report import report

        verify(out)
        print(json.dumps(report(out), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
