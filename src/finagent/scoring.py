"""골든셋 채점. 정답 판정은 전부 코드로 한다(LLM 판정 없음)."""

from __future__ import annotations

import re

from .validate import first_mention, unit_values

REFUSAL = (
    "죄송",
    "없",
    "모르",
    "어렵",
    "제공하지",
    "알 수",
    "확인할 수",
    "답변드릴 수",
)


def _kind(v: float) -> str:
    """정답 값의 단위 종류: 1000 이상이면 원(won), 아니면 %(pct)."""
    return (
        "won" if v >= 1000 else "pct"
    )  # 골든셋 정답은 금리(%) 아니면 원 단위 금액이다


def _negated(ans: str, v: float) -> bool:
    """답이 그 값을 '…가 아니'라고 부정했는지 본다."""
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
    """텍스트의 숫자를 모두 float 목록으로 뽑는다(쉼표 제거)."""
    return [
        float(x) for x in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))
    ]


def _squash(s: str) -> str:
    """공백을 지우고 소문자로 바꾼다(이름 비교용)."""
    return re.sub(r"\s+", "", s).lower()


def score(item: dict, result: dict) -> dict:
    """골든 문항 하나를 채점해 {correct, retrieval_hit}를 돌려준다. 채점 방식은 item['check']['type']을 따른다."""
    ans, chk = result["answer"], item["check"]
    kind, vals = chk["type"], chk.get("values", [])
    q = item.get("query", "")
    if (
        kind == "nums_all"
    ):  # 모든 정답 값이 단위와 함께 있고 부정되지 않아야 한다
        correct = all(
            round(v, 2) in asserted_values(ans, q, _kind(v))
            and not _negated(ans, v)
            for v in vals
        )
    elif (
        kind == "nums_any"
    ):  # 답이 처음 주장한 같은 단위 값이 정답이어야 한다(뒤에 다른 값으로 뒤집는 답 방지)
        first = asserted_values(ans, q, _kind(vals[0]))[:1]
        correct = (
            bool(first)
            and first[0] in {round(v, 2) for v in vals}
            and not _negated(ans, first[0])
        )
    elif kind == "names_any":
        correct = any(_squash(v) in _squash(ans) for v in vals)
    elif kind == "top_choice":
        correct = first_mention(ans, chk["candidates"]) in {
            _squash(v) for v in vals
        }
    elif kind == "text_any":
        correct = any(_squash(v) in _squash(ans) for v in vals)
    elif kind == "refusal":
        correct = any(m in ans for m in REFUSAL) and not any(
            n >= 1000 for n in numbers(ans)
        )
    elif kind == "nonempty":
        correct = bool(ans.strip())
    elif kind == "recommendation":
        picked = first_mention(ans, chk["candidates"])
        winners_ok = (
            {p["id"] for p in result["products"]} == set(chk["product_ids"])
            if "products" in result and "product_ids" in chk
            else picked in {_squash(v) for v in vals}
            and all(_squash(v) in _squash(ans) for v in vals)
        )
        correct = (
            winners_ok
            and chk["value"] in asserted_values(ans, q, chk["unit"])
            and not _negated(ans, chk["value"])
            and (
                not chk.get("terms_any")
                or any(t in ans for t in chk["terms_any"])
            )
        )
    elif kind == "clarification":
        correct = all(
            any(_squash(v) in _squash(ans) for v in group)
            for group in chk["groups"]
        )
        if chk.get("forbid_guarantee"):
            correct = correct and not re.search(
                r"(?:무조건|반드시|누구나).{0,12}(?:가입됩니다|가입가능합니다|수익보장|받을수있습니다)",
                _squash(ans),
            )
    else:
        raise ValueError(f"알 수 없는 채점 방식: {kind}")
    gold, need = item.get("gold_ids") or [], item.get("retrieval")
    if not gold or need is None:
        hit = None
    else:
        got = set(result["retrieved"])
        hit = (
            all(g in got for g in gold)
            if need == "all"
            else any(g in got for g in gold)
        )
    return {"correct": correct, "retrieval_hit": hit}
