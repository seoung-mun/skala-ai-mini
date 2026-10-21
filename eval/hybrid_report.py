"""Render saved hybrid judgments without changing their answers or verdicts."""

import json
from collections import Counter, defaultdict
from pathlib import Path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _counts(rows):
    return {
        "count": len(rows),
        "unique_input_count": len({r["input_key"] for r in rows}),
        "old_original_correct": sum(r["old_original_correct"] for r in rows),
        "old_audited_correct": sum(r["old_audited_correct"] for r in rows),
        "verdict": {
            s: sum(r["verdict"] == s for r in rows)
            for s in ("pass", "fail", "review")
        },
        "numeric": {
            s: sum(r["numeric"] == s for r in rows)
            for s in ("pass", "fail", "review", "not_applicable")
        },
        "semantic": {
            s: sum(r["semantic"] == s for r in rows)
            for s in ("pass", "fail", "review")
        },
        "arithmetic": {
            s: sum(r.get("arithmetic", "not_applicable") == s for r in rows)
            for s in ("pass", "fail", "review", "not_applicable")
        },
        "verdict_disagreement": sum(
            bool(r["verdict_disagreement"]) for r in rows
        ),
    }


def _transition(rows, old_field):
    return dict(
        sorted(
            Counter(
                ("pass" if r[old_field] else "fail") + "->" + r["verdict"]
                for r in rows
            ).items()
        )
    )


def _cost(out, pricing):
    result = {}
    aliases = {
        "input": ("input_per_million", "input_usd_per_million", "input"),
        "cached_input": (
            "cached_input_per_million",
            "cached_input_usd_per_million",
            "cached_input",
        ),
        "output": ("output_per_million", "output_usd_per_million", "output"),
    }
    rates = {
        name: next((pricing[k] for k in keys if k in pricing), None)
        for name, keys in aliases.items()
    }
    for scope in ("calibration", "production"):
        totals = Counter()
        elapsed = 0.0
        for path in sorted((out / "judge" / scope).glob("*.json")):
            call = _read(path)
            totals["saved_calls"] += 1
            totals["error_calls"] += bool(call.get("error"))
            elapsed += float(call.get("elapsed_s", 0) or 0)
            usage = (call.get("raw_response") or {}).get("usage")
            if not usage:
                totals["calls_without_usage"] += 1
                continue
            prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
            cached = (
                usage.get("prompt_tokens_details")
                or usage.get("input_tokens_details")
                or {}
            ).get("cached_tokens", 0)
            if not 0 <= cached <= prompt:
                raise ValueError(f"Invalid cached-token usage: {path}")
            totals["input_tokens"] += prompt
            totals["cached_input_tokens"] += cached
            totals["uncached_input_tokens"] += prompt - cached
            totals["output_tokens"] += usage.get(
                "completion_tokens", usage.get("output_tokens", 0)
            )
        estimate = None
        if all(v is not None for v in rates.values()):
            estimate = (
                totals["uncached_input_tokens"] * float(rates["input"])
                + totals["cached_input_tokens"] * float(rates["cached_input"])
                + totals["output_tokens"] * float(rates["output"])
            ) / 1_000_000
        result[scope] = {
            **{
                k: totals[k]
                for k in (
                    "saved_calls",
                    "error_calls",
                    "calls_without_usage",
                    "input_tokens",
                    "cached_input_tokens",
                    "uncached_input_tokens",
                    "output_tokens",
                )
            },
            "summed_call_elapsed_s": elapsed,
            "estimated_usd": estimate,
            "estimate_complete": estimate is not None
            and not totals["calls_without_usage"],
        }
    result["rates_usd_per_million"] = rates
    estimates = [
        result[s]["estimated_usd"] for s in ("calibration", "production")
    ]
    result["estimated_total_usd"] = (
        sum(estimates) if all(v is not None for v in estimates) else None
    )
    result["estimate_complete"] = all(
        result[s]["estimate_complete"] for s in ("calibration", "production")
    )
    return result


def _quote(text):
    return (
        "\n".join("> " + line for line in str(text).splitlines()) or "> (없음)"
    )


def report(out: Path) -> dict:
    """Create four exclusive report artifacts and return their summary."""
    out = Path(out)
    targets = [
        out / name
        for name in ("summary.json", "changes.json", "report.md", "changes.md")
    ]
    if any(p.exists() for p in targets):
        raise FileExistsError(
            "Report outputs already exist; refusing to overwrite"
        )
    manifest = _read(out / "manifest.json")
    refs = _read(out / "references.json")
    calibration = _read(out / "calibration.json")
    legacy_calibration = (
        _read(out / "legacy_calibration.json")
        if (out / "legacy_calibration.json").exists()
        else None
    )
    integrity = _read(out / "integrity.json")
    systems, changes = {}, []
    all_rows = []
    for system in ("baseline", "current"):
        rows = [
            json.loads(line)
            for line in (out / f"{system}_hybrid.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        if (
            len(rows) != 120
            or len({(r["id"], r["repeat"]) for r in rows}) != 120
        ):
            raise ValueError(
                f"{system}: expected 120 distinct question/repeat rows"
            )
        if len({r["id"] for r in rows}) != 40:
            raise ValueError(f"{system}: expected forty questions")
        if any(r["verdict"] not in {"pass", "fail", "review"} for r in rows):
            raise ValueError(f"{system}: invalid final verdict")
        stats = _counts(rows)
        passed, review = stats["verdict"]["pass"], stats["verdict"]["review"]
        stats["score"] = {
            "denominator": 120,
            "confirmed_pass": passed,
            "lower": passed / 120,
            "upper": (passed + review) / 120,
            "lower_fraction": f"{passed}/120",
            "upper_fraction": f"{passed + review}/120",
        }
        stats["transitions_from_audited"] = _transition(
            rows, "old_audited_correct"
        )
        stats["transitions_from_original"] = _transition(
            rows, "old_original_correct"
        )
        stats["changed_from_audited_count"] = sum(
            r["verdict"] != ("pass" if r["old_audited_correct"] else "fail")
            for r in rows
        )
        stats["changed_from_original_count"] = sum(
            r["verdict"] != ("pass" if r["old_original_correct"] else "fail")
            for r in rows
        )
        stats["categories"] = {
            category: _counts([r for r in rows if r["category"] == category])
            for category in sorted({r["category"] for r in rows})
        }
        stats["all_three_pass"] = sum(
            all(r["verdict"] == "pass" for r in rows if r["id"] == qid)
            for qid in {r["id"] for r in rows}
        )
        systems[system] = stats
        all_rows.extend(rows)
        for row in rows:
            old = "pass" if row["old_audited_correct"] else "fail"
            original = "pass" if row["old_original_correct"] else "fail"
            if old != row["verdict"] or original != row["verdict"]:
                changes.append(
                    {
                        **row,
                        "system": system,
                        "old_audited_status": old,
                        "old_original_status": "pass"
                        if row["old_original_correct"]
                        else "fail",
                        "changed_from_audited": old != row["verdict"],
                        "changed_from_original": original != row["verdict"],
                        "reference": refs[row["id"]],
                    }
                )
    limitations = [
        "각 시스템의 120건은 같은 40문항을 3회 실행한 결과이며 독립적인 120문항이 아니다.",
        "기존 저장 답변을 사후 재채점했으므로 점수 변화는 서비스 실행 성능 향상을 뜻하지 않는다.",
        "핵심 수치·계산·대상/조건 연결·요구 결과만 정오에 반영한다. 표현·순서·의역·요청하지 않은 부연 생략은 실패가 아니다. Q30은 카드 10만원, 실적기간, 마케팅 동의 유지 같은 적용조건을 요구하며 쿠폰 언급은 선택이다.",
        "같은 입력의 중복 답변은 판정 캐시를 공유한다. 중복 행의 동일 판정은 독립 반복 판정의 분산 근거가 아니다.",
        "review를 분모에서 제외하지 않았다. 하한은 pass/120, 상한은 (pass+review)/120이며 신뢰구간이 아니다.",
        "pass는 동결 평가기의 일치 판정이며 사람이 확인한 정답을 뜻하지 않는다. 점수 범위는 review 처리만 반영하며 두 judge가 함께 오판할 가능성은 포함하지 않는다.",
        "코드 검산은 reference 수치와 judge가 원문에서 추출한 산술 관계에 적용한다. 관계 추출 및 일반 의미 판정은 LLM에 의존하므로 모든 자연어 산술 주장을 완전 검증한 것은 아니다.",
        "비용은 manifest의 표준 모델 단가와 저장된 실제 usage로 추정한다. usage 없는 호출의 비용·세금·할인·청구서와의 차이는 포함하지 않는다.",
    ]
    summary = {
        "model": manifest.get("model")
        or manifest.get("settings", {}).get("model"),
        "protocol": manifest.get("protocol", manifest.get("rules", [])),
        "settings": manifest.get("settings", {}),
        "systems": systems,
        "overall": _counts(all_rows),
        "calibration": calibration,
        "legacy_calibration": legacy_calibration,
        "integrity": integrity,
        "cost": _cost(out, manifest.get("pricing", {})),
        "limitations": limitations,
    }
    paired = {
        name: {
            (r["id"], r["repeat"]): r["verdict"]
            for r in all_rows
            if r["system"] == name
        }
        for name in ("baseline", "current")
    }
    summary["paired_transitions"] = dict(
        sorted(
            Counter(
                paired["baseline"][key] + "->" + paired["current"][key]
                for key in paired["baseline"]
            ).items()
        )
    )
    b, c = (systems[name]["score"] for name in ("baseline", "current"))
    summary["improvement_percentage_points"] = {
        "confirmed_pass_difference": 100 * (c["lower"] - b["lower"]),
        "review_sensitivity_lower": 100 * (c["lower"] - b["upper"]),
        "review_sensitivity_upper": 100 * (c["upper"] - b["lower"]),
        "not_a_confidence_interval": True,
    }
    md = [
        "# 저장 답변 핵심 정확성 재채점",
        "",
        "수치·단위의 코드 검증과 의미 판정을 결합한 사후 재채점 결과입니다.",
        "",
        f"모델: `{summary['model']}` · calibration 통과: `{calibration.get('passed')}`",
        "",
        "calibration은 원자료로 정오답을 정의한 합성 개발 점검이며 별도 사람 평가나 독립 holdout 정확도가 아닙니다.",
        f"새 평가기 기대 판정 일치: {calibration.get('matched')}/{calibration.get('total')}개, 각 2회 독립 판정.",
        (
            f"같은 합성 사례의 기존 코드 평가기 일치: {legacy_calibration['matched']}/{legacy_calibration['total']}개."
            if legacy_calibration
            else ""
        ),
        "",
        "| 시스템 | 원래 정답 | 입력 기준 정답 | pass | fail | review | 점수 하한–상한 | 고유 입력 |",
        "|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for name, stats in systems.items():
        v, score = stats["verdict"], stats["score"]
        md.append(
            f"| {name} | {stats['old_original_correct']}/120 | {stats['old_audited_correct']}/120 | "
            f"{v['pass']} | {v['fail']} | {v['review']} | {score['lower']:.2%}–{score['upper']:.2%} "
            f"({score['lower_fraction']}–{score['upper_fraction']}) | {stats['unique_input_count']} |"
        )
    md.extend(
        [
            "",
            f"최신−베이스라인 확정 통과율 차이: {summary['improvement_percentage_points']['confirmed_pass_difference']:+.2f}%p. 보류 처리에 따른 차이 범위: {summary['improvement_percentage_points']['review_sensitivity_lower']:+.2f}~{summary['improvement_percentage_points']['review_sensitivity_upper']:+.2f}%p(신뢰구간 아님).",
            f"같은 질문의 3회 모두 pass: baseline {systems['baseline']['all_three_pass']}/40, current {systems['current']['all_three_pass']}/40.",
            f"동일 질문·반복 대응 판정: `{json.dumps(summary['paired_transitions'], ensure_ascii=False)}`",
            "",
            "## 범주별 판정",
            "",
            "| 시스템/범주 | 건수 | 원래/감사 정답 | pass/fail/review | 수치 pass/fail/review/N.A. | 의미 pass/fail/review | 고유 입력 |",
            "|---|---:|---:|---|---|---|---:|",
        ]
    )
    for name, stats in systems.items():
        for category, cat in stats["categories"].items():
            nums = "/".join(
                str(cat["numeric"][s])
                for s in ("pass", "fail", "review", "not_applicable")
            )
            verdicts = "/".join(
                str(cat["verdict"][s]) for s in ("pass", "fail", "review")
            )
            meanings = "/".join(
                str(cat["semantic"][s]) for s in ("pass", "fail", "review")
            )
            md.append(
                f"| {name}/{category} | {cat['count']} | {cat['old_original_correct']}/{cat['old_audited_correct']} | "
                f"{verdicts} | {nums} | {meanings} | {cat['unique_input_count']} |"
            )
    md.extend(["", "## 변경 및 판정 불확실성", ""])
    for name, stats in systems.items():
        md.append(
            f"- {name}: 감사 기준 변경 {stats['changed_from_audited_count']}건, 원래 기준 변경 "
            f"{stats['changed_from_original_count']}건, 두 judge 최종 판정 불일치 {stats['verdict_disagreement']}건."
        )
        md.append(
            f"  - 감사→신규: `{json.dumps(stats['transitions_from_audited'], ensure_ascii=False)}`"
        )
        md.append(
            f"  - 원래→신규: `{json.dumps(stats['transitions_from_original'], ensure_ascii=False)}`"
        )
    md.extend(
        [
            "",
            "상세 변경은 [changes.md](changes.md), 기계 판독 결과는 [summary.json](summary.json)에 있습니다. 실제 답변 대조에서 확인한 평가기 오판과 보류 사유는 analysis.md에 별도로 기록합니다.",
            "",
            "## 비용 추정",
            "",
            "아래 비용은 이 결과를 구성하는 실제 응답 전체의 usage 추정이며, 이전 실행에서 그대로 재사용한 응답도 포함합니다. 이번 실행의 추가 청구액이 아니며 버전별 비용을 단순 합산하면 중복됩니다. 개발 실행의 응답과 캐시 이력은 각 버전 폴더에 보존했습니다.",
            "```json",
            json.dumps(summary["cost"], ensure_ascii=False, indent=2),
            "```",
            "",
            f"단가 출처(manifest): {manifest.get('pricing', {}).get('source', '미기재')}",
            "",
            "## 해석의 한계",
            "",
        ]
        + [f"- {line}" for line in limitations]
        + [
            "",
            "원본 무결성 결과는 integrity.json 및 summary.json의 integrity에 그대로 포함했습니다.",
        ]
    )
    detail = [
        "# 기존 원본 또는 감사 기준에서 판정이 바뀐 모든 사례",
        "",
        "원본 채점 또는 수정 감사 채점 중 하나라도 달라진 사례를 포함합니다. 동일 시스템·문항·입력 키·감사 판정·최종 판정은 묶고 반복 번호를 모두 표시합니다.",
        "",
    ]
    groups = defaultdict(list)
    for row in changes:
        groups[
            (
                row["system"],
                row["id"],
                row["input_key"],
                row["old_audited_status"],
                row["verdict"],
            )
        ].append(row)
    for (system, qid, key, old, new), rows in sorted(groups.items()):
        row = rows[0]
        detail.extend(
            [
                f"## {system} / {qid} / 원래 {'·'.join(sorted({r['old_original_status'] for r in rows}))} · 감사 {old} → 신규 {new}",
                "",
                f"입력 키: `{key}` · 반복 번호: `{', '.join(str(r['repeat']) for r in rows)}`",
                "",
                "질문:",
                _quote(row["question"]),
                "",
                "이력:",
                "```json",
                json.dumps(row["history"], ensure_ascii=False, indent=2),
                "```",
                "",
                "답변 전문:",
                _quote(row["answer"]),
                "",
                "필수 수치 사실:",
                "```json",
                json.dumps(
                    [f for f in row["reference"]["facts"] if f["required"]],
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
                "의미 요구사항:",
            ]
        )
        detail.extend(
            _quote(s) for s in row["reference"]["semantic_requirements"]
        )
        detail.extend(
            [
                "",
                "원자료 근거:",
                "```json",
                json.dumps(
                    row["reference"]["source_evidence"],
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
            ]
        )
        for current in rows:
            detail.extend(
                [
                    f"### 반복 {current['repeat']}의 판정 근거",
                    "",
                    f"원래 판정: `{current['old_original_status']}` · 수치 `{current['numeric']}` · 의미 `{current['semantic']}`",
                    "",
                ]
            )
            for n, judge in enumerate(current["judge_runs"], 1):
                detail.append(
                    f"Judge {n}: `{judge['verdict']}` · 의미 스키마 유효 `{judge.get('semantic_schema_valid')}`"
                )
                if judge.get("error"):
                    detail.extend(
                        ["", "판정 호출 오류:", _quote(judge["error"]), ""]
                    )
                if not judge.get("semantic_schema_valid"):
                    detail.extend(
                        [
                            "",
                            "아래 개별 의미 판정은 원문 인용 또는 스키마 검증을 통과하지 못한 judge 출력이며, 유효한 의미 채점으로 사용하지 않았습니다.",
                            "",
                        ]
                    )
                failed = [
                    c
                    for c in [
                        *judge.get("numeric_checks", []),
                        *judge.get("arithmetic_checks", []),
                    ]
                    if c.get("verdict") != "pass"
                ]
                detail.extend(
                    [
                        "",
                        "코드 실패·검토 항목:",
                        "```json",
                        json.dumps(failed, ensure_ascii=False, indent=2),
                        "```",
                        "",
                    ]
                )
                for check in judge.get("semantic_checks", []):
                    detail.extend(
                        [
                            f"- {check['dimension']}: **{check['verdict']}** — {check['reason']}"
                        ]
                    )
                    for quote in check.get("quotes", []):
                        detail.extend(["", _quote(quote), ""])
    if not changes:
        detail.append("기존 두 채점 기준과 달라진 사례가 없습니다.")
    artifacts = [
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        json.dumps(changes, ensure_ascii=False, indent=2) + "\n",
        "\n".join(md) + "\n",
        "\n".join(detail) + "\n",
    ]
    for path, content in zip(targets, artifacts):
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
    return summary
