"""골든셋 채점. 정답 판정은 전부 코드로 한다(LLM 판정 없음)."""
from __future__ import annotations

import re

from .validate import first_mention

REFUSAL = ("죄송", "없", "모르", "어렵", "제공하지", "알 수", "확인할 수", "답변드릴 수")


def numbers(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))]


def _has_num(text: str, v: float) -> bool:
    return any(abs(n - v) < 1e-6 for n in numbers(text))


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def score(item: dict, result: dict) -> dict:
    ans, chk = result["answer"], item["check"]
    kind, vals = chk["type"], chk.get("values", [])
    if kind == "nums_all":
        correct = all(_has_num(ans, v) for v in vals)
    elif kind == "nums_any":
        correct = any(_has_num(ans, v) for v in vals)
    elif kind == "names_any":
        correct = any(_squash(v) in _squash(ans) for v in vals)
    elif kind == "top_choice":
        correct = first_mention(ans, chk["candidates"]) in {_squash(v) for v in vals}
    elif kind == "text_any":
        correct = any(_squash(v) in _squash(ans) for v in vals)
    elif kind == "refusal":
        correct = any(m in ans for m in REFUSAL) and not any(n >= 1000 for n in numbers(ans))
    elif kind == "nonempty":
        correct = bool(ans.strip())
    else:
        raise ValueError(f"알 수 없는 채점 방식: {kind}")
    gold, need = item.get("gold_ids") or [], item.get("retrieval")
    if not gold or need is None:
        hit = None
    else:
        got = set(result["retrieved"])
        hit = all(g in got for g in gold) if need == "all" else any(g in got for g in gold)
    return {"correct": correct, "retrieval_hit": hit}
