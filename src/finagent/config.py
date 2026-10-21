"""`.env`를 한 번 읽어 고정하는 설정

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
    """환경변수 값을 읽는다. 비었거나 .env.example 안내 문구 그대로면 None."""
    v = (os.getenv(name) or "").strip()
    return None if not v or v == PLACEHOLDER else v


def _flag(name: str, default: str = "0") -> bool:
    """환경변수를 bool로 읽는다(1·true·yes·on이면 True)."""
    return os.getenv(name, default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class Config:
    """앱 설정 값(불변). load_config()로 만든다."""

    finlife_api_key: str | None
    llm_backend: str  # ollama | openai
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
    supervisor_version: str  # v1 | v2
    rerank: bool  # 크로스인코더 리랭킹(메모리 ~0.4GB)
    planner: str  # rule | llm — hard 질문 작업 분해(LLM Planner + 코드 게이트)
    rag_agent: str  # fixed | react — 근거 검색 서브에이전트(create_agent 툴콜링, 모델 호출 최대 3회)
    openai_reasoning: str | None = (
        None  # 작성 모델 추론 강도(minimal|low|medium|high). 추론 모델일 때만
    )
    agent_model: str | None = (
        None  # 계획자·검색 에이전트 모델. 비우면 작성 모델과 같다
    )
    agent_reasoning: str | None = None  # 계획자·검색 에이전트 추론 강도

    def require(self, field: str, purpose: str) -> str:
        """필수 설정 값을 돌려준다. 비어 있으면 어디에 필요한지 담아 RuntimeError."""
        value = getattr(self, field)
        if not value:
            raise RuntimeError(
                f"{field.upper()}가 .env에 필요합니다({purpose}). .env.example을 참고하세요."
            )
        return value


def load_config(env_file: Path = ROOT / ".env") -> Config:
    """.env를 읽어 검증한 Config를 만든다. 허용되지 않는 값이면 RuntimeError."""
    load_dotenv(
        env_file
    )  # LANGSMITH_* 는 os.environ에 들어가 langsmith SDK가 직접 읽는다
    from langsmith.utils import get_env_var

    tracing = _flag("LANGSMITH_TRACING")
    # SDK는 소문자 'true'만 인식한다. 앱의 bool 해석과 맞추고 로드 전 환경 캐시도 갱신한다.
    os.environ["LANGSMITH_TRACING"] = "true" if tracing else "false"
    get_env_var.cache_clear()
    backend = os.getenv("LLM_BACKEND", "ollama").strip()
    if backend not in ("ollama", "openai"):
        raise RuntimeError(
            f"LLM_BACKEND는 ollama 또는 openai여야 합니다: {backend}"
        )
    version = os.getenv("SUPERVISOR_VERSION", "v2").strip()
    if version not in ("v1", "v2"):
        raise RuntimeError(
            f"SUPERVISOR_VERSION은 v1 또는 v2여야 합니다: {version}"
        )
    planner = os.getenv("PLANNER", "llm").strip()
    if planner not in ("rule", "llm"):
        raise RuntimeError(f"PLANNER는 rule 또는 llm이어야 합니다: {planner}")
    rag_agent = os.getenv("RAG_AGENT", "react").strip()
    if rag_agent not in ("fixed", "react"):
        raise RuntimeError(
            f"RAG_AGENT는 fixed 또는 react여야 합니다: {rag_agent}"
        )
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
        langsmith_tracing=tracing,
        langsmith_api_key=_key("LANGSMITH_API_KEY"),
        tavily_api_key=_key("TAVILY_API_KEY"),
        ecos_api_key=_key("ECOS_API_KEY"),
        supervisor_version=version,
        rerank=_flag("RERANK", "1"),
        planner=planner,
        rag_agent=rag_agent,
        openai_reasoning=_key("OPENAI_REASONING"),
        agent_model=_key("AGENT_MODEL"),
        agent_reasoning=_key("AGENT_REASONING"),
    )
