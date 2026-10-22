"""실제 SDK·어댑터에 HTTP 실패 응답을 주입해 사용량 보존을 확인한다."""

import asyncio
import json

import httpx
import pytest
from langchain_openai import ChatOpenAI

from finagent.llm import OllamaLLM, OpenAILLM
from finagent.schemas import AnswerInput, AnswerOutput, LLMOutputError


def completion(content, *, finish_reason="stop", refusal=None, usage=True):
    body = {
        "id": "chat-usage-test",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4.1-mini",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "refusal": refusal,
                },
            }
        ],
    }
    if usage:
        body["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
        }
    return body


@pytest.mark.parametrize(
    "response",
    [
        completion("not json"),
        completion('{"ok":true,"reply":"답","citations":[]}'),
        completion(None, refusal="refused"),
        completion("", finish_reason="length"),
        completion("", finish_reason="content_filter"),
        completion(
            '{"ok":true,"reply":"답","citations":[],"missing":"","extra":1}'
        ),
        completion('{"ok":true,"reply":"답","citations":[],"missing":""'),
    ],
    ids=[
        "json",
        "schema",
        "refusal",
        "length",
        "filter",
        "extra-field",
        "truncated",
    ],
)
def test_openai_output_errors_preserve_observed_usage(response):
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        llm = OpenAILLM("gpt-4.1-mini", api_key="test")
        llm.client = ChatOpenAI(
            model="gpt-4.1-mini",
            api_key="test",
            http_client=client,
            max_retries=0,
        )
        with pytest.raises(LLMOutputError) as caught:
            llm.structured("sys", AnswerInput(question="q"), AnswerOutput)

    assert caught.value.usage == {
        "model": "openai:gpt-4.1-mini",
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "tokens": 110,
        "usd": 0.000056,
    }
    schema = sent["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["name"] == "AnswerOutput"
    assert schema["schema"]["additionalProperties"] is False


@pytest.mark.parametrize("structured", [True, False])
def test_openai_missing_usage_still_fails(structured):
    response = completion(
        '{"ok":true,"reply":"답","citations":[],"missing":""}',
        usage=False,
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response)
        )
    ) as client:
        llm = OpenAILLM("gpt-4.1-mini", api_key="test")
        llm.client = ChatOpenAI(
            model="gpt-4.1-mini",
            api_key="test",
            http_client=client,
            max_retries=0,
        )
        with pytest.raises(LLMOutputError, match="사용량") as caught:
            if structured:
                llm.structured("sys", AnswerInput(question="q"), AnswerOutput)
            else:
                llm.chat("sys", "q", schema=AnswerOutput.model_json_schema())
    assert caught.value.usage is None


@pytest.mark.parametrize("structured", [True, False])
def test_ollama_parse_error_preserves_observed_usage(structured):
    from langchain_ollama import ChatOllama

    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "done": True,
                "message": {"role": "assistant", "content": "not json"},
                "prompt_eval_count": 100,
                "eval_count": 10,
                "load_duration": 2_000_000,
                "prompt_eval_duration": 3_000_000,
                "eval_duration": 4_000_000,
            },
        )

    llm = OllamaLLM("qwen3:8b", 0.005)
    llm.client = ChatOllama(
        model="qwen3:8b",
        client_kwargs={"transport": httpx.MockTransport(handler)},
    )
    with pytest.raises(LLMOutputError) as caught:
        if structured:
            llm.structured("sys", AnswerInput(question="q"), AnswerOutput)
        else:
            llm.chat("sys", "q", schema=AnswerOutput.model_json_schema())
    assert caught.value.usage["prompt_tokens"] == 100
    assert caught.value.usage["completion_tokens"] == 10
    assert caught.value.usage["usd"] == 0.00055
    assert caught.value.usage["gen_ms"] == 4


@pytest.mark.parametrize(
    "failed", [completion("not json"), completion("", finish_reason="length")]
)
def test_graph_retry_totals_include_failed_http_response(failed):
    from test_graph import CAT, A, B, FakeMCP, FakeRetriever, route_reply

    from finagent.deps import Deps
    from finagent.graph.build import build_graph, run_v2

    responses = [
        completion(
            json.dumps(route_reply(kinds=["saving"], products=["가나적금"]))
        ),
        failed,
        completion(
            json.dumps(
                {
                    "ok": True,
                    "reply": "가나적금은 급여 이체 시 연 0.5%p 우대됩니다.",
                    "citations": [],
                    "missing": "",
                },
                ensure_ascii=False,
            )
        ),
    ]
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(sent) - 1])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        llm = OpenAILLM("gpt-4.1-mini", api_key="test")
        llm.client = ChatOpenAI(
            model="gpt-4.1-mini",
            api_key="test",
            http_client=client,
            max_retries=0,
        )
        deps = Deps(
            llm,
            FakeMCP(),
            FakeRetriever([A, B]),
            CAT,
            "202609",
            "rule",
            "fixed",
        )
        final, state = asyncio.run(
            run_v2(build_graph(deps), deps, "가나적금 우대조건 알려줘")
        )

    assert state["verified"] is True
    assert len(sent) == final["metadata"]["llm_calls"] == 3
    assert final["metadata"]["prompt_tokens"] == 300
    assert final["metadata"]["completion_tokens"] == 30
    assert final["metadata"]["cost_usd"] == 0.000168
    assert [run["prompt_tokens"] for run in state["writing_runs"]] == [100, 100]
    assert [run["completion_tokens"] for run in state["writing_runs"]] == [
        10,
        10,
    ]
