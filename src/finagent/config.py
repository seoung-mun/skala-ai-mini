"""`.env`를 한 번 읽어 고정하는 설정 (전남 클론 fixed/config.py 패턴).

비밀 값은 출력하지 않는다. `.env.example`의 안내 문구가 그대로 남아 있으면 키가 없는 것으로 본다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER = "여기에-키-입력"


def _key(name: str) -> str | None:
    v = (os.getenv(name) or "").strip()
    return None if not v or v == PLACEHOLDER else v


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    finlife_api_key: str | None
    llm_backend: str            # ollama | openai
    ollama_url: str
    ollama_model: str
    ollama_num_ctx: int
    openai_api_key: str | None
    openai_base_url: str | None
    openai_model: str
    langsmith_tracing: bool
    langsmith_api_key: str | None
    tavily_api_key: str | None
    ecos_api_key: str | None
    supervisor_version: str     # v1 | v2
    rerank: bool                # 크로스인코더 리랭킹(메모리 ~0.4GB)

    def require(self, field: str, purpose: str) -> str:
        value = getattr(self, field)
        if not value:
            raise RuntimeError(f"{field.upper()}가 .env에 필요합니다({purpose}). .env.example을 참고하세요.")
        return value


def load_config(env_file: Path = ROOT / ".env") -> Config:
    load_dotenv(env_file)  # LANGSMITH_* 는 os.environ에 들어가 langsmith SDK가 직접 읽는다
    backend = os.getenv("LLM_BACKEND", "ollama").strip()
    if backend not in ("ollama", "openai"):
        raise RuntimeError(f"LLM_BACKEND는 ollama 또는 openai여야 합니다: {backend}")
    version = os.getenv("SUPERVISOR_VERSION", "v2").strip()
    if version not in ("v1", "v2"):
        raise RuntimeError(f"SUPERVISOR_VERSION은 v1 또는 v2여야 합니다: {version}")
    return Config(
        finlife_api_key=_key("FINLIFE_API_KEY"),
        llm_backend=backend,
        ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
        # 문맥 길이만큼 KV 캐시를 미리 잡는다(16384면 qwen3:8b가 7.4GB). 실측 최대 프롬프트 ~3.5K라 8192로 충분.
        ollama_num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "8192")),
        openai_api_key=_key("OPENAI_API_KEY"),
        openai_base_url=_key("OPENAI_BASE_URL"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        langsmith_tracing=_flag("LANGSMITH_TRACING"),
        langsmith_api_key=_key("LANGSMITH_API_KEY"),
        tavily_api_key=_key("TAVILY_API_KEY"),
        ecos_api_key=_key("ECOS_API_KEY"),
        supervisor_version=version,
        rerank=_flag("RERANK", "1"),
    )
