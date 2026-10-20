"""골든셋 채점. 정답 판정은 전부 코드로 한다(LLM 판정 없음)."""
from __future__ import annotations

import re

from .validate import first_mention, unit_values

REFUSAL = ("죄송", "없", "모르", "어렵", "제공하지", "알 수", "확인할 수", "답변드릴 수")


def _kind(v: float) -> str:
    return "won" if v >= 1000 else "pct"  # 골든셋 정답은 금리(%) 아니면 원 단위 금액이다


def _negated(ans: str, v: float) -> bool:
    num = f"{v:,.2f}".rstrip("0").rstrip(".").replace(",", ",?")
    return bool(re.search(rf"{num}\s*(%p|%|원)?\s*(가|이)\s*아니", ans))


def asserted_values(ans: str, query: str, kind: str) -> list[float]:
    """답이 '주장'한 같은 단위 값들(등장 순서). 질문에 나온 값(입력 금액·금리)을 되풀이한 것은 뺀다."""
    echoed = {v for _, k, v in unit_values(query) if k == kind}
    if kind == "won":
        from .slots import won_amounts
        echoed |= {float(x) for x in won_amounts(query)}
    return [v for _, k, v in unit_values(ans) if k == kind and v not in echoed]


def numbers(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))]



def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def score(item: dict, result: dict) -> dict:
    ans, chk = result["answer"], item["check"]
    kind, vals = chk["type"], chk.get("values", [])
    q = item.get("query", "")
    if kind == "nums_all":  # 모든 정답 값이 단위와 함께 있고 부정되지 않아야 한다
        correct = all(round(v, 2) in asserted_values(ans, q, _kind(v)) and not _negated(ans, v) for v in vals)
    elif kind == "nums_any":  # 답이 처음 주장한 같은 단위 값이 정답이어야 한다(뒤에 다른 값으로 뒤집는 답 방지)
        first = asserted_values(ans, q, _kind(vals[0]))[:1]
        correct = bool(first) and first[0] in {round(v, 2) for v in vals} and not _negated(ans, first[0])
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
