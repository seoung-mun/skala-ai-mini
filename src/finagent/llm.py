"""LangChain LLM 어댑터. 그래프의 structured()와 베이스라인의 chat() 인터페이스를 유지한다."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TypeVar

import httpx
from langchain_core.exceptions import OutputParserException
from ollama import ResponseError
from openai import (
    APIError,
    APITimeoutError,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
)
from pydantic import BaseModel, ValidationError

from .schemas import LLMOutputError

T = TypeVar("T", bound=BaseModel)


def parse_output(text: str, out: type[T]) -> T:
    """JSON 텍스트를 출력 모델로 검증한다. 형식이 틀리면 LLMOutputError."""
    try:
        return out.model_validate_json(text)
    except ValidationError as e:
        raise LLMOutputError(
            f"{out.__name__} 형식 오류({e.error_count()}건, 길이 {len(text)}, 끝 {text[-50:]!r})"
        ) from e


@contextmanager
def llm_errors(usage=None):
    """SDK 오류를 앱 오류 종류(시간 초과·연결 실패·형식 오류)로 바꾼다. create_agent 호출도 같이 쓴다."""
    try:
        yield
    except APITimeoutError as e:
        raise TimeoutError("OpenAI 응답 시간 초과") from e
    except (APIError, ResponseError, ConnectionError) as e:
        # SDK 오류 본문에 인증 정보가 포함될 수 있어 사용자에게는 종류만 전달한다.
        raise httpx.HTTPError(f"LLM 호출 실패: {type(e).__name__}") from e
    except (
        ValidationError,
        OutputParserException,
        LengthFinishReasonError,
        ContentFilterFinishReasonError,
    ) as e:
        completion = getattr(e, "completion", None)
        observed = (
            usage(completion)
            if usage and completion is not None and completion.usage is not None
            else None
        )
        raise LLMOutputError(
            f"LLM 구조화 응답 오류: {type(e).__name__}", usage=observed
        ) from e


def _invoke(model, messages, *, usage=None, **kwargs):
    """모델을 호출하고 SDK 오류를 앱 오류 종류로 바꾼다."""
    with llm_errors(usage):
        return model.invoke(messages, **kwargs)


def _structured(model, schema, messages, *, usage, **options):
    """원본 사용량을 확보한 뒤 스키마를 검증해 (파싱 결과, 원본, 사용량)을 돌려준다."""
    out = (
        schema
        if isinstance(schema, type) and issubclass(schema, BaseModel)
        else None
    )
    runnable = model.with_structured_output(
        # SDK가 먼저 Pydantic 검증하면 오류 응답의 원본·사용량이 사라진다.
        out.model_json_schema() if out else schema,
        method="json_schema",
        include_raw=True,
        **options,
    )
    result = _invoke(runnable, messages, usage=usage)
    observed = usage(result["raw"])
    if result["parsing_error"] is not None or result["parsed"] is None:
        raise LLMOutputError(
            "LLM 구조화 응답이 스키마를 따르지 않거나 응답을 거절했습니다",
            usage=observed,
        ) from result["parsing_error"]
    try:
        parsed = (
            out.model_validate_json(result["raw"].text)
            if out
            else result["parsed"]
        )
    except ValidationError as e:
        raise LLMOutputError(
            "LLM 구조화 응답 오류: ValidationError", usage=observed
        ) from e
    return parsed, result["raw"], observed


def _usage(message, name: str, usd_in: float, usd_out: float) -> dict:
    """응답 메시지의 토큰 사용량과 비용(USD)을 계산한다. 사용량이 없으면 LLMOutputError."""
    if hasattr(message, "usage_metadata"):
        usage = message.usage_metadata
    else:  # OpenAI SDK의 길이·필터 오류에는 원본 completion이 붙는다.
        raw = message.usage
        usage = (
            {
                "input_tokens": raw.prompt_tokens,
                "output_tokens": raw.completion_tokens,
            }
            if raw is not None
            else None
        )
    if usage is None:
        raise LLMOutputError("LLM 토큰 사용량이 응답에 없습니다")
    p, c = usage["input_tokens"], usage["output_tokens"]
    return {
        "model": name,
        "prompt_tokens": p,
        "completion_tokens": c,
        "tokens": p + c,
        "usd": round((p * usd_in + c * usd_out) / 1000, 6),
    }


class OllamaLLM:
    """LangChain ChatOllama 기반 로컬 LLM 어댑터(chat·structured)."""

    def __init__(
        self,
        model: str,
        usd_per_1k_tokens: float,
        url: str = "http://127.0.0.1:11434/api/chat",
        num_ctx: int = 16384,
        num_predict: int = 1024,
        think: bool = False,
    ):
        """ChatOllama 클라이언트를 만든다(temperature 0, 문맥·출력 토큰 상한 지정)."""
        from langchain_ollama import ChatOllama

        self.name = f"ollama:{model}"
        self.model, self.usd_per_1k, self.url = model, usd_per_1k_tokens, url
        self.num_ctx, self.num_predict, self.think = num_ctx, num_predict, think
        self.client = ChatOllama(
            model=model,
            base_url=url.removesuffix("/api/chat"),
            temperature=0,
            num_ctx=num_ctx,
            num_predict=num_predict,
            reasoning=think,
            client_kwargs={"timeout": 600.0},
        )

    def usage(self, message) -> dict:
        """토큰 사용량과 단계별 소요 시간(ms)을 돌려준다. 프롬프트가 num_ctx에 닿으면 RuntimeError(잘림 의심)."""
        usage = _usage(message, self.name, self.usd_per_1k, self.usd_per_1k)
        if usage["prompt_tokens"] >= self.num_ctx:
            raise RuntimeError(
                f"프롬프트 {usage['prompt_tokens']}토큰이 num_ctx {self.num_ctx}에 닿았습니다(잘림 의심)"
            )
        meta = message.response_metadata
        usage.update(
            {
                label: round((meta.get(key) or 0) / 1e6)
                for label, key in (
                    ("load_ms", "load_duration"),
                    ("prompt_ms", "prompt_eval_duration"),
                    ("gen_ms", "eval_duration"),
                )
            }
        )
        return usage

    def chat(
        self, system: str, user: str, schema: dict | None = None
    ) -> tuple[str, dict]:
        """일반 호출. schema가 있으면 그 JSON Schema로 출력을 강제한다. (텍스트, 사용량)을 돌려준다."""
        messages = [("system", system), ("human", user)]
        if schema is None:
            message = _invoke(
                self.client, messages, usage=self.usage, stream=False
            )
            usage = self.usage(message)
        else:
            _, message, usage = _structured(
                self.client, schema, messages, usage=self.usage
            )
        return message.text, usage

    def structured(
        self, system: str, payload: BaseModel, out: type[T]
    ) -> tuple[T, dict]:
        """입력 모델을 프롬프트로 보내고 출력 모델로 파싱해 (결과, 사용량)을 돌려준다."""
        parsed, _, usage = _structured(
            self.client,
            out,
            [("system", system), ("human", payload.to_prompt())],
            usage=self.usage,
        )
        return parsed, usage


# USD/1K 토큰 (입력, 출력). OpenAI 공개 단가 — 목록에 없는 모델은 비용을 지어내지 않고 시작할 때 실패한다.
OPENAI_PRICES = {
    "gpt-4.1": (0.002, 0.008),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1-nano": (0.0001, 0.0004),
    "o4-mini": (0.0011, 0.0044),
    "o3-mini": (0.0011, 0.0044),
    "gpt-5": (0.00125, 0.01),
    "gpt-5-mini": (0.00025, 0.002),
    "gpt-5-nano": (0.00005, 0.0004),
}
REASONING_EFFORTS = ("minimal", "low", "medium", "high")
OPENAI_TIMEOUT_S = 90
# 출력 폭주(무한 반복)를 수 초 안에 LengthFinishReasonError → LLMOutputError(반려·대체)로 끊는다.
# 추론 토큰도 출력 한도에 포함되므로 추론 모델은 여유를 더 준다.
MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS_REASONING = 2048, 8192


class OpenAILLM:
    """init_chat_model로 만든 ChatOpenAI의 JSON Schema 출력·파싱을 사용한다.
    reasoning이 있으면 추론(thinking) 모델로 보고 reasoning_effort를 넘긴다 — 추론 모델은 temperature를 받지 않는다.
    추론 토큰은 completion_tokens에 포함돼 비용에 잡힌다."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        reasoning: str | None = None,
    ):
        """모델 단가·추론 강도를 검사하고 ChatOpenAI 클라이언트를 만든다. 모르는 값이면 RuntimeError."""
        from langchain.chat_models import init_chat_model

        if model not in OPENAI_PRICES:
            raise RuntimeError(
                f"{model} 단가를 모릅니다. llm.OPENAI_PRICES에 (입력, 출력) USD/1K를 추가하세요."
            )
        if reasoning is not None and reasoning not in REASONING_EFFORTS:
            raise RuntimeError(
                f"추론 강도는 {'|'.join(REASONING_EFFORTS)} 중 하나여야 합니다: {reasoning}"
            )
        self.name = f"openai:{model}" + (f"@{reasoning}" if reasoning else "")
        self.model, (self.usd_in, self.usd_out) = model, OPENAI_PRICES[model]
        sampling = (
            {
                "reasoning_effort": reasoning,
                "max_tokens": MAX_OUTPUT_TOKENS_REASONING,
            }
            if reasoning
            else {"temperature": 0, "max_tokens": MAX_OUTPUT_TOKENS}
        )
        # SDK 기본(600초·재시도 2회)이면 한 호출이 수 분 멈춰 보인다. 추론 모델 여유를 두고 상한을 명시한다.
        self.client = init_chat_model(
            model,
            model_provider="openai",
            api_key=api_key,
            use_responses_api=False,
            timeout=OPENAI_TIMEOUT_S,
            max_retries=1,
            base_url=base_url or "https://api.openai.com/v1",
            **sampling,
        )

    def usage(self, message) -> dict:
        """토큰 사용량과 비용(USD)을 돌려준다."""
        return _usage(message, self.name, self.usd_in, self.usd_out)

    def chat(
        self,
        system: str,
        user: str,
        schema: dict | None = None,
        schema_name: str = "Output",
    ) -> tuple[str, dict]:
        """일반 호출. schema가 있으면 strict JSON Schema로 출력을 강제한다. (텍스트, 사용량)을 돌려준다."""
        messages = [("system", system), ("human", user)]
        if schema is None:
            message = _invoke(self.client, messages, usage=self.usage)
            usage = self.usage(message)
        else:
            _, message, usage = _structured(
                self.client,
                {**schema, "title": schema_name},
                messages,
                usage=self.usage,
                strict=True,
            )
        return message.text, usage

    def structured(
        self, system: str, payload: BaseModel, out: type[T]
    ) -> tuple[T, dict]:
        """strict JSON Schema로 호출해 (출력 모델, 사용량)을 돌려준다."""
        parsed, _, usage = _structured(
            self.client,
            out,
            [("system", system), ("human", payload.to_prompt())],
            usage=self.usage,
            strict=True,
        )
        return parsed, usage


def build_llm(cfg):
    """설정에 따라 답변 작성 LLM을 만든다. openai인데 키가 없으면 명확한 메시지로 실패한다."""
    if cfg.llm_backend == "openai":
        return OpenAILLM(
            cfg.openai_model,
            cfg.require("openai_api_key", "LLM_BACKEND=openai"),
            cfg.openai_base_url,
            cfg.openai_reasoning,
        )
    return OllamaLLM(
        cfg.ollama_model,
        usd_per_1k_tokens=0.005,
        url=cfg.ollama_url,
        num_ctx=cfg.ollama_num_ctx,
    )


def build_agent_llm(cfg, writer):
    """계획자·검색 에이전트용 LLM. AGENT_MODEL·AGENT_REASONING이 비어 있으면 작성 LLM을 같이 쓴다."""
    if cfg.llm_backend != "openai" or not (
        cfg.agent_model or cfg.agent_reasoning
    ):
        return writer
    return OpenAILLM(
        cfg.agent_model or cfg.openai_model,
        cfg.require("openai_api_key", "AGENT_MODEL"),
        cfg.openai_base_url,
        cfg.agent_reasoning,
    )
