"""run_eval 결과(jsonl)에서 도구·서브에이전트 호출과 재작성·검증을 집계한다(LLM 호출 없음).

uv run python eval/tool_report.py eval/results/openai/gpt-4.1-mini/<실행>/v3.jsonl
"""

import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path


def main(path: Path) -> None:
    """결과 jsonl을 서브에이전트(도구)별 호출·성공·지연 표와 재작성·검증 집계로 출력한다."""
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if not any("tasks" in r for r in rows):
        raise SystemExit(
            "이 결과에는 tasks 기록이 없습니다(도구별 집계는 tasks를 "
            "남기는 run_eval로 측정한 결과만 가능)"
        )
    by_agent = defaultdict(list)
    for r in rows:
        for t in r.get("tasks", []):
            by_agent[t["agent"]].append({**t, "item": r["id"]})
    print(f"{path}: {len(rows)}행, 정답 {sum(r['correct'] for r in rows)}\n")
    print(
        "| 서브에이전트(도구) | 호출 | 성공 | 실패 | 재시도 | 지연 "
        "중앙값(ms) | 최대(ms) |"
    )
    print("|---|---|---|---|---|---|---|")
    for agent, ts in sorted(by_agent.items()):
        ms = [t["elapsed_ms"] for t in ts]
        print(
            f"| {agent} | {len(ts)} | {sum(t['ok'] for t in ts)} | {sum(not t['ok'] for t in ts)} | "
            f"{sum(t['retries'] for t in ts)} | {st.median(ms):.0f} | {max(ms)} |"
        )
    fails = [t for ts in by_agent.values() for t in ts if not t["ok"]]
    for t in fails:
        print(f"- 실패 {t['item']} {t['agent']}: {t['error']}")
    rw = [r for r in rows if r.get("rewrite")]
    print(
        f"\n후속 질문 재작성 시도 {len(rw)}건, 적용 {sum(r['rewrite']['applied'] for r in rw)}건"
    )
    for r in rw:
        w = r["rewrite"]
        print(
            f"- {r['id']} {'O' if r['correct'] else 'X'} {'적용' if w['applied'] else '미적용'}: "
            f"{w['original']!r} → {w['question']!r} ({w['reason']})"
        )
    va = Counter(r["validation_attempts"] for r in rows)
    print(
        f"\n검증 시도 횟수 분포: {dict(sorted(va.items()))}, 검증 실패로 표 대체: "
        f"{sum(1 for r in rows if r.get('verified') is False)}건"
    )
    slow = sorted(rows, key=lambda r: -r["latency_s"])[:5]
    print(
        "느린 문항: "
        + ", ".join(
            f"{r['id']} {r['latency_s']}s(llm {r['llm_calls']})" for r in slow
        )
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
