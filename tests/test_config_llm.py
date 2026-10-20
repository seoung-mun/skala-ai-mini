import json
from types import SimpleNamespace

import pytest

from finagent.config import PLACEHOLDER, load_config
from finagent.llm import OpenAILLM, parse_output
from finagent.schemas import AnswerInput, AnswerOutput, LLMOutputError

KEYS = ("FINLIFE_API_KEY", "LLM_BACKEND", "OPENAI_API_KEY", "LANGSMITH_TRACING", "SUPERVISOR_VERSION", "TAVILY_API_KEY")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    return lambda text: (f.write_text(text), load_config(f))[1]


def test_placeholder_means_missing_and_require_explains(env):
    cfg = env(f"FINLIFE_API_KEY={PLACEHOLDER}\nTAVILY_API_KEY=abc\n")
    assert cfg.finlife_api_key is None and cfg.tavily_api_key == "abc" and cfg.llm_backend == "ollama"
    with pytest.raises(RuntimeError, match="FINLIFE_API_KEY"):
        cfg.require("finlife_api_key", "수집")


def test_invalid_backend_or_version_fails_loudly(env):
    with pytest.raises(RuntimeError, match="LLM_BACKEND"):
        env("LLM_BACKEND=gemini\n")


def test_env_example_lists_every_key_config_reads():
    from pathlib import Path
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    for k in KEYS + ("OPENAI_BASE_URL", "OPENAI_MODEL", "OLLAMA_MODEL", "OLLAMA_URL", "OLLAMA_NUM_CTX", "LANGSMITH_API_KEY",
                     "LANGSMITH_PROJECT", "ECOS_API_KEY", "RERANK"):
        assert f"{k}=" in example, k


def test_parse_output_rejects_wrong_shape():
    assert parse_output('{"ok": true, "reply": "hi", "citations": [], "missing": ""}', AnswerOutput).reply == "hi"
    with pytest.raises(LLMOutputError):  # 필수 필드 누락(빈 출력)도 형식 오류다
        parse_output('{"ok": true}', AnswerOutput)
    with pytest.raises(LLMOutputError, match="AnswerOutput"):
        parse_output('{"reply": 3}', AnswerOutput)


def test_openai_adapter_sends_schema_and_parses_with_fake_client():
    llm = OpenAILLM("gpt-x", api_key="test")
    sent = {}

    def create(**kw):
        sent.update(kw)
        msg = SimpleNamespace(content=json.dumps({"ok": True, "reply": "답", "citations": [], "missing": ""}))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10))

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    out, usage = llm.structured("sys", AnswerInput(question="q"), AnswerOutput)
    assert out.reply == "답" and usage["usd"] == round(0.1 * 0.0004 + 0.01 * 0.0016, 6)
    assert sent["response_format"]["json_schema"]["name"] == "AnswerOutput"
    assert sent["messages"][1]["content"].startswith("## 질문\nq")
    assert sent["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


def test_output_schemas_forbid_extra_keys_and_input_renders_sections():
    from finagent.schemas import ChatOutput, Description, ProductDoc, RouteOutput
    for m in (AnswerOutput, RouteOutput, ChatOutput):
        assert m.model_json_schema()["additionalProperties"] is False
    with pytest.raises(Exception):
        AnswerOutput.model_validate({"ok": True, "reply": "x", "citations": [], "missing": "", "made_up_key": 1})
    doc = ProductDoc(product_id="p1", title="가은행 적금", info={"금융권": "은행"}, rate_table="| 12 | 3.0 |",
                     descriptions=[Description(section="우대조건", text="급여 이체 시 0.5%p")])
    text = AnswerInput(question="q", documents=[doc], citable_ids=["p1"], previous_rejection="숫자 틀림").to_prompt()
    assert "### 금리표 (인용 금지)" in text and "- [우대조건] 급여 이체 시 0.5%p" in text and "숫자 틀림" in text
