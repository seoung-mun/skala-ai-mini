import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval/run_eval.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def _row(gid, query, answer, correct):
    return {"id": gid, "query": query, "difficulty": "easy", "category": "fact", "answer": answer,
            "retrieved": [], "route": "baseline", "latency_s": 1.0, "llm_calls": 1, "retries": 0,
            "prompt_tokens": 1, "completion_tokens": 1, "usd": 0.0, "repeat": 0, "correct": correct,
            "retrieval_hit": None, "check": {}, "gold_ids": [], "retrieval": None}


def test_rescore_rescores_saved_answers_without_llm(tmp_path, monkeypatch):
    (tmp_path / "eval/results").mkdir(parents=True)
    golden = {"id": "E1", "query": "금리?", "difficulty": "easy", "category": "fact", "gold_ids": [], "retrieval": None,
              "check": {"type": "nums_any", "values": [2.45]}}
    (tmp_path / "eval/golden.jsonl").write_text(json.dumps(golden, ensure_ascii=False) + "\n")
    # 예전 채점기에서는 정답이던 답(부정 후 다른 값)이 새 채점기에서 오답으로 바뀌어야 한다
    (tmp_path / "eval/results/baseline.jsonl").write_text(
        json.dumps(_row("E1", "금리?", "2.45%가 아니라 99.9%입니다", True), ensure_ascii=False) + "\n")
    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(R, "RES", tmp_path / "eval/results")
    R.rescore()
    row = json.loads((tmp_path / "eval/results/baseline.jsonl").read_text())
    assert row["correct"] is False
    assert json.loads((tmp_path / "eval/results/baseline_summary.json").read_text())["accuracy"] == 0.0


def test_rescore_refuses_when_question_changed(tmp_path, monkeypatch):
    (tmp_path / "eval/results").mkdir(parents=True)
    golden = {"id": "E1", "query": "새 질문", "difficulty": "easy", "category": "fact", "gold_ids": [],
              "retrieval": None, "check": {"type": "nums_any", "values": [1.0]}}
    (tmp_path / "eval/golden.jsonl").write_text(json.dumps(golden, ensure_ascii=False) + "\n")
    (tmp_path / "eval/results/baseline.jsonl").write_text(json.dumps(_row("E1", "옛 질문", "1%", True)) + "\n")
    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(R, "RES", tmp_path / "eval/results")
    with pytest.raises(SystemExit, match="재측정"):
        R.rescore()
