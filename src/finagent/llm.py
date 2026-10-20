"""LLM 어댑터. 그래프·베이스라인은 chat(system, user) 인터페이스에만 의존한다(테스트에서는 가짜 LLM 주입)."""
from __future__ import annotations

import httpx
from langsmith import traceable

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


class OllamaLLM:
    def __init__(self, model: str, usd_per_1k_tokens: float, num_ctx: int = 16384, think: bool = False,
                 num_predict: int = 1024):
        self.name = f"ollama:{model}"
        self.model, self.usd_per_1k, self.num_ctx, self.think = model, usd_per_1k_tokens, num_ctx, think
        self.num_predict = num_predict

    @traceable(run_type="llm", name="ollama.chat")
    def chat(self, system: str, user: str, schema: dict | None = None) -> tuple[str, dict]:
        """schema를 주면 Ollama 구조화 출력(JSON Schema)으로 응답을 강제한다."""
        # num_ctx 기본값(4096)을 넘는 문서는 Ollama가 조용히 잘라내므로 명시한다.
        payload = {"model": self.model, "stream": False, "think": self.think,
                   "options": {"temperature": 0.0, "num_ctx": self.num_ctx, "num_predict": self.num_predict},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if schema is not None:
            payload["format"] = schema
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=600.0)
        resp.raise_for_status()
        d = resp.json()
        prompt_toks, gen_toks = d.get("prompt_eval_count") or 0, d.get("eval_count") or 0
        if prompt_toks >= self.num_ctx:
            raise RuntimeError(f"프롬프트 {prompt_toks}토큰이 num_ctx {self.num_ctx}에 닿았습니다(잘림 의심)")
        tokens = prompt_toks + gen_toks
        return d["message"]["content"], {"model": self.name, "prompt_tokens": prompt_toks,
                                         "completion_tokens": gen_toks, "tokens": tokens,
                                         "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
