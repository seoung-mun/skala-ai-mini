import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "run_eval", ROOT / "eval/run_eval.py"
)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def _row(gid, query, answer, correct):
    """테스트용 측정 결과 행을 만든다."""
    return {
        "id": gid,
        "query": query,
        "difficulty": "easy",
        "category": "fact",
        "answer": answer,
        "retrieved": [],
        "route": "baseline",
        "latency_s": 1.0,
        "llm_calls": 1,
        "retries": 0,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "usd": 0.0,
        "repeat": 0,
        "correct": correct,
        "retrieval_hit": None,
        "check": {},
        "gold_ids": [],
        "retrieval": None,
    }


def test_rescore_rescores_saved_answers_without_llm(tmp_path, monkeypatch):
    """rescore는 저장된 답을 LLM 없이 다시 채점한다."""
    (tmp_path / "eval/results").mkdir(parents=True)
    golden = {
        "id": "E1",
        "query": "금리?",
        "difficulty": "easy",
        "category": "fact",
        "gold_ids": [],
        "retrieval": None,
        "check": {"type": "nums_any", "values": [2.45]},
    }
    (tmp_path / "eval/golden.jsonl").write_text(
        json.dumps(golden, ensure_ascii=False) + "\n"
    )
    # 예전 채점기에서는 정답이던 답(부정 후 다른 값)이 새 채점기에서 오답으로 바뀌어야 한다
    (tmp_path / "eval/results/baseline.jsonl").write_text(
        json.dumps(
            _row("E1", "금리?", "2.45%가 아니라 99.9%입니다", True),
            ensure_ascii=False,
        )
        + "\n"
    )
    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(R, "RES", tmp_path / "eval/results")
    R.rescore()
    row = json.loads((tmp_path / "eval/results/baseline.jsonl").read_text())
    assert row["correct"] is False
    assert (
        json.loads(
            (tmp_path / "eval/results/baseline_summary.json").read_text()
        )["accuracy"]
        == 0.0
    )


def test_rescore_refuses_when_question_changed(tmp_path, monkeypatch):
    """골든 질문이 바뀌었으면 rescore를 거부한다."""
    (tmp_path / "eval/results").mkdir(parents=True)
    golden = {
        "id": "E1",
        "query": "새 질문",
        "difficulty": "easy",
        "category": "fact",
        "gold_ids": [],
        "retrieval": None,
        "check": {"type": "nums_any", "values": [1.0]},
    }
    (tmp_path / "eval/golden.jsonl").write_text(
        json.dumps(golden, ensure_ascii=False) + "\n"
    )
    (tmp_path / "eval/results/baseline.jsonl").write_text(
        json.dumps(_row("E1", "옛 질문", "1%", True)) + "\n"
    )
    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(R, "RES", tmp_path / "eval/results")
    with pytest.raises(SystemExit, match="재측정"):
        R.rescore()


def config(
    backend="openai",
    model="gpt-4.1-mini",
    planner="llm",
    rag_agent="react",
    agent_reasoning=None,
):
    """결과 경로 분리에 쓰는 가짜 설정 객체."""
    return SimpleNamespace(
        llm_backend=backend,
        openai_model=model,
        ollama_model="qwen3:8b",
        rerank=True,
        ollama_num_ctx=8192,
        planner=planner,
        rag_agent=rag_agent,
        openai_reasoning=None,
        agent_model=None,
        agent_reasoning=agent_reasoning,
    )


def test_results_are_partitioned_and_reject_mixed_models(tmp_path, monkeypatch):
    """결과는 모델·설정별 디렉터리로 나뉘고 다른 모델 결과와 섞이지 않는다."""
    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(R, "RES", tmp_path / "eval/results")
    first = R.prepare_results(config())
    second = R.prepare_results(config())
    assert first != second
    assert first.parent == tmp_path / "eval/results/openai/gpt-4.1-mini"
    assert R.prepare_results(config(), first) == first
    with pytest.raises(SystemExit, match="다른 결과 디렉터리"):
        R.prepare_results(config(model="another-model"), first)
    with pytest.raises(SystemExit, match="다른 결과 디렉터리"):
        R.prepare_results(
            config(planner="rule"), first
        )  # 계획기·검색 에이전트가 다른 측정도 섞지 않는다
    with pytest.raises(SystemExit, match="다른 결과 디렉터리"):
        R.prepare_results(
            config(agent_reasoning="low"), first
        )  # 추론 강도도 마찬가지
    assert "api_key" not in (first / "manifest.json").read_text()


def test_legacy_results_are_never_silently_relabelled(tmp_path, monkeypatch):
    """과거 결과가 있는 디렉터리는 새 측정 결과 디렉터리로 쓰지 않고 원본을 그대로 둔다."""
    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(R, "RES", tmp_path / "eval/results")
    (tmp_path / "baseline.jsonl").write_text("original")
    with pytest.raises(SystemExit, match="과거 결과"):
        R.prepare_results(config(), tmp_path)
    assert (tmp_path / "baseline.jsonl").read_text() == "original"


def test_measurement_checkpoints_resume_without_overwriting(
    tmp_path, monkeypatch
):
    """중단된 측정은 기존 결과를 덮지 않고 이어서 측정한다."""
    import sys

    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(R, "RES", tmp_path / "eval/results")
    (tmp_path / "eval").mkdir()
    item = {
        "id": "E1",
        "query": "금리?",
        "difficulty": "easy",
        "category": "fact",
        "gold_ids": [],
        "retrieval": None,
        "check": {"type": "nums_any", "values": [2.45]},
    }
    (tmp_path / "eval/golden.jsonl").write_text(json.dumps(item))
    result_dir = tmp_path / "isolated"
    monkeypatch.setattr(R, "load_config", config)
    calls = []

    async def run(system, golden, repeat, checkpoint, completed):
        """첫 호출은 한 행을 기록한 뒤 중단되고, 이후 호출은 남은 반복만 기록하는 가짜 run_system."""
        calls.append(completed.copy())
        row = {
            **_row("E1", "금리?", "2.45%", True),
            **item,
            "model": "openai:gpt-4.1-mini",
        }
        if not completed:
            checkpoint(row)
            raise RuntimeError("interrupted")
        row = {**row, "repeat": 1}
        checkpoint(row)
        return [row]

    monkeypatch.setattr(R, "run_system", run)
    args = [
        "run_eval.py",
        "baseline",
        "--repeat",
        "2",
        "--results-dir",
        str(result_dir),
    ]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(RuntimeError, match="interrupted"):
        R.main()
    original = (result_dir / "baseline.jsonl").read_text()
    with pytest.raises(SystemExit):
        R.main()
    assert (result_dir / "baseline.jsonl").read_text() == original
    monkeypatch.setattr(sys, "argv", args + ["--missing"])
    R.main()
    assert calls[-1] == {("E1", 0)}
    assert len((result_dir / "baseline.jsonl").read_text().splitlines()) == 2
    assert (
        json.loads((result_dir / "baseline_run.json").read_text())["status"]
        == "complete"
    )


def test_summary_keeps_single_request_tail_latency_visible():
    """요약에 단일 요청의 꼬리 지연(최대값)이 드러난다."""
    rows = [
        {
            **_row(f"E{i}", "금리?", "2.45%", True),
            "latency_s": 1 if i < 9 else 215.7,
        }
        for i in range(10)
    ]
    summary = R.summarize(rows)
    assert summary["latency_median_s"] == 1
    assert summary["latency_p90_s"] == 1
    assert summary["latency_max_s"] == 215.7
