"""감독 노드의 구조화 LLM 호출과 성공·실패 사용량 집계."""

import asyncio

from ..schemas import LLMOutputError


async def _llm(deps, system: str, payload, out, llm=None, timing=None):
    """구조화 LLM 호출. (출력 모델, 사용량 델타)를 돌려준다. 형식 오류는 LLMOutputError로 올린다."""
    try:
        result, u = await asyncio.to_thread(
            (llm or deps.llm).structured, system, payload, out
        )
    except LLMOutputError as e:
        if timing is not None and e.usage:
            timing.update(
                {
                    k: v
                    for k, v in e.usage.items()
                    if k
                    in (
                        "prompt_tokens",
                        "completion_tokens",
                        "load_ms",
                        "prompt_ms",
                        "gen_ms",
                    )
                }
            )
        raise
    if timing is not None:
        timing.update(
            {
                k: u[k]
                for k in (
                    "prompt_tokens",
                    "completion_tokens",
                    "load_ms",
                    "prompt_ms",
                    "gen_ms",
                )
                if k in u
            }
        )
    return result, {
        "llm_calls": 1,
        "prompt_tokens": u["prompt_tokens"],
        "completion_tokens": u["completion_tokens"],
        "cost_usd": u["usd"],
    }


def _failed_usage(error: LLMOutputError) -> dict:
    usage = error.usage or {}
    return {
        "llm_calls": 1,
        **{
            label: usage[key]
            for key, label in (
                ("prompt_tokens", "prompt_tokens"),
                ("completion_tokens", "completion_tokens"),
                ("usd", "cost_usd"),
            )
            if key in usage
        },
    }


def _add(*deltas: dict) -> dict:
    """LLM 사용량 델타를 합친다(한 노드에서 LLM을 두 번 부를 때)."""
    out: dict = {}
    for d in deltas:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out
