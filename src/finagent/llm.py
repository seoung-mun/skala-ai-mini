"""LLM 어댑터. v2 노드는 structured(system, 입력모델, 출력모델)만 쓰고, v1 베이스라인은 chat(문자열)을 쓴다.

출력은 pydantic 모델의 JSON Schema로 형식을 강제하고 model_validate_json으로 검증한다.
"""
from __future__ import annotations

from typing import TypeVar

import httpx
from langsmith import traceable
from pydantic import BaseModel, ValidationError

from .schemas import LLMOutputError

T = TypeVar("T", bound=BaseModel)


def parse_output(text: str, out: type[T]) -> T:
    try:
        return out.model_validate_json(text)
    except ValidationError as e:
        raise LLMOutputError(f"{out.__name__} 형식 오류({e.error_count()}건, 길이 {len(text)}, 끝 {text[-50:]!r})") from e


class OllamaLLM:
    def __init__(self, model: str, usd_per_1k_tokens: float, url: str = "http://127.0.0.1:11434/api/chat",
                 num_ctx: int = 16384, num_predict: int = 1024, think: bool = False):
        self.name = f"ollama:{model}"
        self.model, self.usd_per_1k, self.url = model, usd_per_1k_tokens, url
        self.num_ctx, self.num_predict, self.think = num_ctx, num_predict, think

    @traceable(run_type="llm", name="ollama.chat")
    def chat(self, system: str, user: str, schema: dict | None = None) -> tuple[str, dict]:
        # num_ctx 기본값(4096)을 넘는 문서는 Ollama가 조용히 잘라내므로 명시한다.
        payload = {"model": self.model, "stream": False, "think": self.think,
                   "options": {"temperature": 0.0, "num_ctx": self.num_ctx, "num_predict": self.num_predict},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if schema is not None:
            payload["format"] = schema
        resp = httpx.post(self.url, json=payload, timeout=600.0)
        resp.raise_for_status()
        d = resp.json()
        p, c = d.get("prompt_eval_count") or 0, d.get("eval_count") or 0
        if p >= self.num_ctx:
            raise RuntimeError(f"프롬프트 {p}토큰이 num_ctx {self.num_ctx}에 닿았습니다(잘림 의심)")
        ms = lambda k: round((d.get(k) or 0) / 1e6)  # Ollama는 ns 단위로 단계별 시간을 준다(지연 분석용)
        return d["message"]["content"], {"model": self.name, "prompt_tokens": p, "completion_tokens": c,
                                         "tokens": p + c, "usd": round((p + c) / 1000 * self.usd_per_1k, 6),
                                         "load_ms": ms("load_duration"), "prompt_ms": ms("prompt_eval_duration"),
                                         "gen_ms": ms("eval_duration")}

    def structured(self, system: str, payload: BaseModel, out: type[T]) -> tuple[T, dict]:
        text, usage = self.chat(system, payload.to_prompt(), out.model_json_schema())
        return parse_output(text, out), usage


class OpenAILLM:
    """OpenAI 호환 API(사내 프록시 포함). 단가는 USD/1K 토큰(입력·출력 따로)."""

    def __init__(self, model: str, api_key: str, base_url: str | None = None,
                 usd_in_per_1k: float = 0.0004, usd_out_per_1k: float = 0.0016):
        from openai import OpenAI  # LLM_BACKEND=openai일 때만 필요
        self.name = f"openai:{model}"
        self.model, self.usd_in, self.usd_out = model, usd_in_per_1k, usd_out_per_1k
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    @traceable(run_type="llm", name="openai.chat")
    def chat(self, system: str, user: str, schema: dict | None = None, schema_name: str = "Output") -> tuple[str, dict]:
        kw = {}
        if schema is not None:
            kw["response_format"] = {"type": "json_schema",
                                     "json_schema": {"name": schema_name, "schema": schema, "strict": False}}
        r = self.client.chat.completions.create(model=self.model, temperature=0, **kw, messages=[
            {"role": "system", "content": system}, {"role": "user", "content": user}])
        p, c = r.usage.prompt_tokens, r.usage.completion_tokens
        return r.choices[0].message.content or "", {
            "model": self.name, "prompt_tokens": p, "completion_tokens": c, "tokens": p + c,
            "usd": round(p / 1000 * self.usd_in + c / 1000 * self.usd_out, 6)}

    def structured(self, system: str, payload: BaseModel, out: type[T]) -> tuple[T, dict]:
        text, usage = self.chat(system, payload.to_prompt(), out.model_json_schema(),
                                out.__name__)
        return parse_output(text, out), usage


def build_llm(cfg):
    """설정에 따라 LLM을 만든다. openai인데 키가 없으면 명확한 메시지로 실패한다."""
    if cfg.llm_backend == "openai":
        return OpenAILLM(cfg.openai_model, cfg.require("openai_api_key", "LLM_BACKEND=openai"), cfg.openai_base_url)
    return OllamaLLM(cfg.ollama_model, usd_per_1k_tokens=0.005, url=cfg.ollama_url, num_ctx=cfg.ollama_num_ctx)
