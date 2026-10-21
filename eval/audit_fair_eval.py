"""응답·정답을 보존하고 발견된 평가기 결함만 별도 파일에 재채점한다."""

import argparse
import hashlib
import inspect
import json
from pathlib import Path

from run_fair_eval import MODEL, append, grade, quantities, report, save, squash


def audit(out):
    items = {
        i["id"]: i
        for i in map(
            json.loads,
            (out / "snapshot/eval/fair_suite_20261021.jsonl")
            .read_text()
            .splitlines(),
        )
    }
    changes = []
    for system in ["baseline", "current"]:
        dest = out / f"{system}_audited.jsonl"
        if dest.exists():
            raise SystemExit("기존 재채점 결과를 덮어쓰지 않습니다")
        for original in map(
            json.loads, (out / f"{system}.jsonl").read_text().splitlines()
        ):
            row = dict(original)
            row["original_evaluation"] = {
                k: original.get(k)
                for k in [
                    "correct",
                    "checks",
                    "status",
                    "error_type",
                    "models_ok",
                    "usage_incomplete",
                ]
            }
            # 최종 응답 반환 후 평가기의 쉼표 파싱에서 실패한 경우에만 보존된 답 복원.
            if (
                row["status"] == "error"
                and row.get("error_type") == "ValueError"
                and row.get("final", {}).get("reply")
            ):
                row["answer"] = row["final"]["reply"]
                row["status"] = "ok"
                row["evaluator_error_recovered"] = True
                row["usage_incomplete"] = any(
                    not c["usage"] for c in row["observed_calls"]
                )
            if row["status"] == "ok":
                row.update(grade(items[row["id"]], row["answer"]))
            row["models_ok"] = all(
                c["model"] == MODEL for c in row["observed_calls"]
            )
            if not row["observed_calls"]:
                row["model_observation"] = (
                    "no model call; deterministic rule path"
                )
            append(dest, row)
            if (
                original["correct"] != row["correct"]
                or original["status"] != row["status"]
            ):
                changes.append(
                    {
                        "system": system,
                        "id": row["id"],
                        "repeat": row["repeat"],
                        "before_correct": original["correct"],
                        "after_correct": row["correct"],
                        "before_status": original["status"],
                        "after_status": row["status"],
                    }
                )
    source = "import re\n\n" + "\n\n".join(
        inspect.getsource(fn) for fn in [squash, quantities, grade]
    )
    (out / "audited_scorer.py").write_text(source)
    save(
        out / "scorer_revision.json",
        {
            "post_hoc": True,
            "changes": [
                "numeric token must start with a digit; bare comma before 원 is not a quantity",
                "recognize 어려워요 and canonical scope refusal",
                "recognize 내년/내후년 as future in refusal intent",
                "no-call deterministic route has no response model to mismatch",
            ],
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "changed_observations": changes,
            "no_new_model_calls": True,
            "questions_oracles_and_original_responses_unchanged": True,
        },
    )
    report(out, audited=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("out", type=Path)
    audit(p.parse_args().out.resolve())
