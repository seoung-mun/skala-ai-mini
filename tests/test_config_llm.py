import json

import httpx
import pytest

from finagent.config import PLACEHOLDER, load_config
from finagent.llm import OllamaLLM, OpenAILLM, parse_output
from finagent.schemas import AnswerInput, AnswerOutput, LLMOutputError

KEYS = (
    "FINLIFE_API_KEY",
    "LLM_BACKEND",
    "OPENAI_API_KEY",
    "LANGSMITH_TRACING",
    "SUPERVISOR_VERSION",
    "TAVILY_API_KEY",
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """.env 내용을 받아 깨끗한 환경에서 load_config()를 부르는 함수를 준다."""
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    return lambda text: (f.write_text(text), load_config(f))[1]


def test_placeholder_means_missing_and_require_explains(env):
    """안내 문구 그대로인 키는 없는 것으로 보고, require()는 키 이름을 담아 실패한다."""
    cfg = env(f"FINLIFE_API_KEY={PLACEHOLDER}\nTAVILY_API_KEY=abc\n")
    assert (
        cfg.finlife_api_key is None
        and cfg.tavily_api_key == "abc"
        and cfg.llm_backend == "ollama"
    )
    with pytest.raises(RuntimeError, match="FINLIFE_API_KEY"):
        cfg.require("finlife_api_key", "수집")


def test_invalid_backend_or_version_fails_loudly(env):
    """허용되지 않는 LLM_BACKEND·버전 값은 RuntimeError로 실패한다."""
    with pytest.raises(RuntimeError, match="LLM_BACKEND"):
        env("LLM_BACKEND=gemini\n")


def test_env_example_lists_every_key_config_reads():
    """.env.example에 설정이 읽는 모든 키가 있다."""
    from pathlib import Path

    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    for k in KEYS + (
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "OLLAMA_MODEL",
        "OLLAMA_URL",
        "OLLAMA_NUM_CTX",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
        "ECOS_API_KEY",
        "RERANK",
        "PLANNER",
        "RAG_AGENT",
        "OPENAI_REASONING",
        "AGENT_MODEL",
        "AGENT_REASONING",
    ):
        assert f"{k}=" in example, k


def test_parse_output_rejects_wrong_shape():
    """parse_output은 필드 누락·잘못된 JSON을 LLMOutputError로 거부한다."""
    assert (
        parse_output(
            '{"ok": true, "reply": "hi", "citations": [], "missing": ""}',
            AnswerOutput,
        ).reply
        == "hi"
    )
    with pytest.raises(LLMOutputError):  # 필수 필드 누락(빈 출력)도 형식 오류다
        parse_output('{"ok": true}', AnswerOutput)
    with pytest.raises(LLMOutputError, match="AnswerOutput"):
        parse_output('{"reply": 3}', AnswerOutput)


@pytest.fixture
def openai_transport(monkeypatch):
    """가짜 HTTP 전송으로 OpenAILLM을 만드는 함수를 준다(실제 API 호출 없음)."""
    from langchain.chat_models import init_chat_model

    clients = []

    def install(handler, model="gpt-4.1-mini", reasoning=None):
        """handler가 응답하는 OpenAILLM을 만든다."""
        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)

        def create(*args, **kwargs):
            """가짜 HTTP 클라이언트를 붙여 init_chat_model을 호출한다."""
            return init_chat_model(
                *args, **{**kwargs, "max_retries": 0}, http_client=client
            )

        monkeypatch.setattr("langchain.chat_models.init_chat_model", create)
        return OpenAILLM(model, api_key="test", reasoning=reasoning)

    yield install
    for client in clients:
        client.close()


def completion(content, refusal=None):
    """OpenAI chat.completion 응답 JSON을 만든다."""
    return {
        "id": "chat-test",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4.1-mini",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "refusal": refusal,
                },
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
        },
    }


def test_openai_langchain_sends_strict_schema_and_keeps_usage(openai_transport):
    """OpenAI 호출이 strict JSON Schema를 보내고 토큰 사용량·비용을 남긴다."""
    sent = {}

    def handler(request):
        """보낸 요청을 기록하고 고정 응답을 돌려준다."""
        sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            json=completion(
                json.dumps(
                    {"ok": True, "reply": "답", "citations": [], "missing": ""}
                )
            ),
        )

    llm = openai_transport(handler)
    out, usage = llm.structured("sys", AnswerInput(question="q"), AnswerOutput)
    assert (
        isinstance(out, AnswerOutput)
        and out.reply == "답"
        and out.missing == ""
    )
    assert usage == {
        "model": "openai:gpt-4.1-mini",
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "tokens": 110,
        "usd": round(0.1 * 0.0004 + 0.01 * 0.0016, 6),
    }
    schema = sent["response_format"]["json_schema"]
    assert schema["name"] == "AnswerOutput" and schema["strict"] is True
    assert set(schema["schema"]["required"]) == {
        "ok",
        "reply",
        "citations",
        "missing",
    }
    assert schema["schema"]["additionalProperties"] is False
    assert sent["messages"][1]["content"].startswith("## 질문\nq")
    assert sent["stream"] is False and sent["temperature"] == 0
    assert llm.client.request_timeout == 90  # 무기한 대기 대신 상한
    assert 2048 in (
        sent.get("max_tokens"),
        sent.get("max_completion_tokens"),
    )  # 출력 폭주는 길이 오류로 끊는다


def test_openai_reasoning_model_sends_effort_without_temperature(
    openai_transport,
):
    """추론 모델은 reasoning_effort를 보내고 temperature는 보내지 않는다."""
    sent = {}

    def handler(request):
        """보낸 요청을 기록하고 고정 응답을 돌려준다."""
        sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            json=completion(
                json.dumps(
                    {"ok": True, "reply": "답", "citations": [], "missing": ""}
                )
            ),
        )

    llm = openai_transport(handler, model="o4-mini", reasoning="low")
    _, usage = llm.structured("sys", AnswerInput(question="q"), AnswerOutput)
    assert (
        sent["reasoning_effort"] == "low"
        and "temperature" not in sent
        and sent["model"] == "o4-mini"
    )
    assert usage["model"] == "openai:o4-mini@low" and usage["usd"] == round(
        0.1 * 0.0011 + 0.01 * 0.0044, 6
    )


def test_openai_unknown_price_or_effort_fails_at_start():
    """단가를 모르는 모델이나 잘못된 추론 강도는 생성 시점에 실패한다."""
    with pytest.raises(RuntimeError, match="단가"):
        OpenAILLM("gpt-unknown", api_key="test")
    with pytest.raises(RuntimeError, match="추론 강도"):
        OpenAILLM("o4-mini", api_key="test", reasoning="max")


def test_openai_plain_chat_remains_unstructured(openai_transport):
    """schema 없는 chat()은 구조화 출력 옵션 없이 보낸다."""
    sent = {}

    def handler(request):
        """보낸 요청을 기록하고 고정 응답을 돌려준다."""
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=completion("일반 답변"))

    text, usage = openai_transport(handler).chat("sys", "질문")
    assert text == "일반 답변" and usage["tokens"] == 110
    assert "response_format" not in sent


@pytest.mark.parametrize(
    "content,refusal",
    [
        ('{"ok":true,"reply":"답","citations":[]}', None),
        ("not json", None),
        (None, "refused"),
    ],
)
def test_openai_schema_failures_and_refusals_remain_llm_output_errors(
    openai_transport, content, refusal
):
    """스키마 위반·거절 응답은 LLMOutputError가 된다."""
    llm = openai_transport(
        lambda request: httpx.Response(200, json=completion(content, refusal))
    )
    with pytest.raises(LLMOutputError):
        llm.structured("sys", AnswerInput(question="q"), AnswerOutput)


@pytest.mark.parametrize("failure", ["timeout", "connection", "status"])
def test_openai_transport_errors_preserve_api_error_codes(
    openai_transport, failure
):
    """전송 오류가 계약 오류 코드로 바뀌고 응답 본문은 메시지에 새지 않는다."""
    from finagent.server import error_code

    def handler(request):
        """지정한 실패(시간 초과·연결 실패·503)를 흉내 낸다."""
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if failure == "connection":
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(
            503,
            json={
                "error": {
                    "message": "private-test-marker",
                    "type": "server_error",
                }
            },
        )

    llm = openai_transport(handler)
    with pytest.raises((TimeoutError, httpx.HTTPError)) as caught:
        llm.structured("sys", AnswerInput(question="q"), AnswerOutput)
    assert error_code(caught.value) == (
        "TIMEOUT" if failure == "timeout" else "LLM_UNAVAILABLE"
    )
    assert "private-test-marker" not in str(caught.value)


def test_openai_empty_base_url_uses_official_endpoint(monkeypatch):
    """OPENAI_BASE_URL이 비어 있으면 공식 엔드포인트를 쓴다."""
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    llm = OpenAILLM("gpt-4.1-mini", api_key="test", base_url=None)
    assert str(llm.client.root_client.base_url) == "https://api.openai.com/v1/"
    llm.client.root_client.close()


@pytest.mark.parametrize("structured", [False, True])
def test_ollama_langchain_preserves_options_timings_and_context_guard(
    structured,
):
    """Ollama 옵션 전달·소요 시간 기록·num_ctx 잘림 검사를 확인한다."""
    from langchain_ollama import ChatOllama

    sent = {}

    def handler(request):
        """보낸 요청을 기록하고 Ollama 형식 고정 응답을 돌려준다."""
        sent.update(json.loads(request.content))
        content = (
            json.dumps(
                {"ok": True, "reply": "답", "citations": [], "missing": ""}
            )
            if structured
            else "답"
        )
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "done": True,
                "message": {"role": "assistant", "content": content},
                "prompt_eval_count": 100,
                "eval_count": 10,
                "load_duration": 2_000_000,
                "prompt_eval_duration": 3_000_000,
                "eval_duration": 4_000_000,
            },
        )

    llm = OllamaLLM(
        "qwen3:8b", 0.005, num_ctx=1000, num_predict=100, think=False
    )
    assert llm.client.base_url == "http://127.0.0.1:11434"
    llm.client = ChatOllama(
        model="qwen3:8b",
        num_ctx=1000,
        num_predict=100,
        reasoning=False,
        temperature=0,
        client_kwargs={"transport": httpx.MockTransport(handler)},
    )
    result, usage = (
        llm.structured("sys", AnswerInput(question="q"), AnswerOutput)
        if structured
        else llm.chat("sys", "q")
    )
    assert (result.reply if structured else result) == "답"
    assert usage["tokens"] == 110 and usage["usd"] == 0.00055
    assert (usage["load_ms"], usage["prompt_ms"], usage["gen_ms"]) == (2, 3, 4)
    assert (
        sent["options"]["num_ctx"] == 1000
        and sent["options"]["num_predict"] == 100
    )
    assert sent["think"] is False
    if structured:
        assert "missing" in sent["format"]["required"]
    llm.num_ctx = 100
    with pytest.raises(RuntimeError, match="잘림 의심"):
        llm.chat("sys", "q")


def test_output_schemas_forbid_extra_keys_and_input_renders_sections():
    """출력 스키마는 추가 키를 금지하고, 입력 모델은 섹션별 텍스트로 렌더링된다."""
    from finagent.schemas import (
        ChatOutput,
        Description,
        ProductDoc,
        RouteOutput,
    )

    for m in (AnswerOutput, RouteOutput, ChatOutput):
        assert m.model_json_schema()["additionalProperties"] is False
    with pytest.raises(Exception):
        AnswerOutput.model_validate(
            {
                "ok": True,
                "reply": "x",
                "citations": [],
                "missing": "",
                "made_up_key": 1,
            }
        )
    doc = ProductDoc(
        product_id="p1",
        title="가은행 적금",
        info={"금융권": "은행"},
        rate_table="| 12 | 3.0 |",
        descriptions=[
            Description(section="우대조건", text="급여 이체 시 0.5%p")
        ],
    )
    text = AnswerInput(
        question="q",
        documents=[doc],
        citable_ids=["p1"],
        previous_rejection="숫자 틀림",
    ).to_prompt()
    assert (
        "### 금리표 (인용 금지)" in text
        and "- [우대조건] 급여 이체 시 0.5%p" in text
        and "숫자 틀림" in text
    )


def test_inverse_calculation_prompt_does_not_call_required_principal_pretax_interest():
    """역산 결과는 프롬프트에서 '세전'으로 표기되지 않고 전제가 함께 나온다."""
    from finagent.schemas import Calculation

    text = AnswerInput(
        question="목표 달성?",
        calculations=[
            Calculation(
                name="목표 달성 월 납입액",
                result=826000,
                inputs={
                    "mode": "goal",
                    "product_type": "saving",
                    "amount_won": 10_000_000,
                    "term_months": 12,
                    "rate_pct": 3.5,
                },
                formula="최소 월 납입액",
            )
        ],
    ).to_prompt()
    assert "목표 달성 월 납입액: 826,000원" in text
    assert (
        "전제: 목표 세후 수령액 10,000,000원 · 12개월 · 연 3.5% · 단리" in text
    )
    assert "세전" not in text


@pytest.mark.parametrize(
    "value,enabled",
    [
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("true", True),
        ("False", False),
        ("false", False),
        ("0", False),
    ],
)
def test_langsmith_flag_matches_sdk_and_refreshes_cached_environment(
    env, monkeypatch, value, enabled
):
    """LANGSMITH_TRACING 해석이 SDK와 같고, 캐시된 환경값도 갱신된다."""
    import os

    from langsmith import utils

    for key in (
        "LANGSMITH_TRACING_V2",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_TRACING",
    ):
        monkeypatch.delenv(key, raising=False)
    utils.get_env_var.cache_clear()
    utils.get_env_var(
        "TRACING", default=""
    )  # .env 로드 전 캐시도 갱신해야 한다
    cfg = env(f"LANGSMITH_TRACING={value}\n")
    assert cfg.langsmith_tracing is enabled
    assert os.environ["LANGSMITH_TRACING"] == ("true" if enabled else "false")
    assert utils.tracing_is_enabled() is enabled
    utils.get_env_var.cache_clear()
