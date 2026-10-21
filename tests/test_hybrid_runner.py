"""Offline integrity and blinding checks; no client or API is created."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "eval"))
    spec = importlib.util.spec_from_file_location(
        "rescore_hybrid_test", ROOT / "eval/rescore_hybrid.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def prepared(runner, monkeypatch, tmp_path):
    source, out = tmp_path / "source", tmp_path / "output"
    (source / "snapshot/eval").mkdir(parents=True)
    suite = [
        {
            "id": f"Q{i:02}",
            "query": f"질문{i}",
            "history": [],
            "category": "rate_fact",
        }
        for i in range(1, 41)
    ]

    def jsonl(path, data):
        path.write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in data)
        )

    jsonl(source / "snapshot/eval/fair_suite_20261021.jsonl", suite)
    original = [
        {
            "id": item["id"],
            "repeat": rep,
            "answer": "3%",
            "correct": True,
            "category": item["category"],
        }
        for item in suite
        for rep in range(3)
    ]
    for system in ["baseline", "current"]:
        jsonl(source / f"{system}.jsonl", original)
        jsonl(source / f"{system}_audited.jsonl", original)
    monkeypatch.setattr(
        runner,
        "build_references",
        lambda _: {
            item["id"]: {
                "facts": [],
                "semantic_requirements": [],
                "source_evidence": [],
            }
            for item in suite
        },
    )
    before = runner.hashes(source)
    runner.initialize(source, out)
    assert runner.hashes(source) == before
    return source, out


def test_prepare_preserves_originals_and_freezes_settings(runner, prepared):
    source, out = prepared
    manifest = runner.verify(out)
    assert manifest["source_sha256"] == runner.hashes(source)
    assert manifest["repetitions"] == 2
    assert manifest["settings"]["model"] == runner.MODEL
    assert manifest["settings"]["reasoning_effort"] == "medium"
    assert len(runner.read(out / "suite.json")) == 40
    for name in runner.FILES:
        assert (out / "evaluator" / name).read_bytes() == (
            ROOT / "eval" / name
        ).read_bytes()
    with pytest.raises(FileExistsError):
        runner.write(out / "references.json", {})


def test_source_or_reference_changes_block_resume(runner, prepared):
    source, out = prepared
    refs_path = out / "references.json"
    original_refs = refs_path.read_text()
    refs_path.write_text("{}")
    with pytest.raises(ValueError, match="reference"):
        runner.verify(out)
    refs_path.write_text(original_refs)
    (source / "current.jsonl").write_text("changed")
    with pytest.raises(ValueError, match="원본"):
        runner.verify(out)


def test_prepare_accepts_original_only_without_creating_audit(runner, prepared):
    source, previous = prepared
    for system in ("baseline", "current"):
        (source / f"{system}_audited.jsonl").unlink()
    before = runner.hashes(source)
    out = previous.parent / "original_only"
    runner.initialize(source, out)
    manifest = runner.verify(out)
    assert manifest["input_files"] == {
        "baseline": "baseline.jsonl",
        "current": "current.jsonl",
    }
    assert runner.hashes(source) == before


def test_payload_blinding_and_cache_key_uses_full_context(runner):
    item = {
        "query": "그 상품 24개월은요?",
        "history": [{"role": "user", "content": "상품A"}],
        "id": "Q31",
        "system": "current",
        "correct": True,
        "final": {"secret": "hidden"},
    }
    reference = {
        "facts": [{"id": "f1", "value": "3", "required": True}],
        "semantic_requirements": ["문맥 유지"],
    }
    data = runner.payload(item, "3%", reference)
    assert set(data) == {"question", "history", "answer", "reference"}
    assert (
        "system" not in data and "correct" not in data and "final" not in data
    )

    def key(p):
        return runner.digest(
            {
                "payload": p,
                "settings": runner.SETTINGS,
                "prompt": runner.SYSTEM,
                "schema": runner.Judgment.model_json_schema(),
            }
        )

    original = key(data)
    assert original == key(
        runner.payload(
            {**item, "system": "baseline", "correct": False}, "3%", reference
        )
    )
    for field, value in [
        ("question", "다른 질문"),
        ("history", []),
        ("answer", "4%"),
        ("reference", {"facts": []}),
    ]:
        changed = copy.deepcopy(data)
        changed[field] = value
        assert key(changed) != original


def test_failed_calibration_blocks_before_client_creation(
    runner, prepared, monkeypatch
):
    _, out = prepared
    runner.write(out / "calibration.json", {"passed": False})
    runner.write(out / "calibration_inputs.json", {})

    def forbidden_client():
        pytest.fail("Calibration failure must block before API initialization")

    monkeypatch.setattr(runner, "make_client", forbidden_client)
    with pytest.raises(ValueError, match="calibration"):
        runner.rescore(out)


def test_changed_frozen_judge_inputs_block_before_client_creation(
    runner, prepared, monkeypatch
):
    _, out = prepared
    runner.write(out / "production_inputs.json", {"key": {"answer": "3%"}})
    monkeypatch.setattr(
        runner,
        "make_client",
        lambda: pytest.fail("Changed frozen inputs must block"),
    )
    with pytest.raises(ValueError, match="입력"):
        runner.run_jobs(out, "production", {"key": {"answer": "4%"}})


def test_frozen_suite_and_copied_calibration_changes_block_resume(
    runner, prepared
):
    _, out = prepared
    suite_path = out / "suite.json"
    original_suite = suite_path.read_text()
    suite = runner.read(suite_path)
    suite[0]["query"] = "변경된 질문"
    suite_path.write_text(json.dumps(suite, ensure_ascii=False))
    with pytest.raises(ValueError):
        runner.verify(out)
    suite_path.write_text(original_suite)
    (out / "evaluator/hybrid_calibration.json").write_text("[]")
    with pytest.raises(ValueError):
        runner.verify(out)


def test_forged_passed_flag_cannot_bypass_calibration(
    runner, prepared, monkeypatch
):
    _, out = prepared
    runner.write(
        out / "calibration.json", {"passed": True, "matched": 999, "total": 999}
    )
    runner.write(out / "calibration_inputs.json", {})
    monkeypatch.setattr(
        runner,
        "make_client",
        lambda: pytest.fail("Forged passed flag must not create API client"),
    )
    with pytest.raises(ValueError, match="calibration"):
        runner.rescore(out)


def test_calibration_requires_two_actual_saved_judge_responses(
    runner, prepared
):
    _, out = prepared
    examples = runner.read(out / "evaluator/hybrid_calibration.json")
    refs = runner.read(out / "references.json")
    items = {item["id"]: item for item in runner.read(out / "suite.json")}
    cases, inputs = [], {}
    for example in examples:
        data = runner.payload(
            items[example["qid"]], example["answer"], refs[example["qid"]]
        )
        key = runner.digest(
            {
                "payload": data,
                "settings": runner.SETTINGS,
                "prompt": runner.SYSTEM,
                "schema": runner.Judgment.model_json_schema(),
            }
        )
        inputs[key] = data
        cases.append({**example, "input_key": key, "judge_runs": []})
    runner.write(out / "calibration.json", {"passed": True, "cases": cases})
    runner.write(out / "calibration_inputs.json", inputs)
    with pytest.raises(ValueError, match="실제 judge 응답"):
        runner.calibration_gate(out)


def test_saved_valid_judge_verdict_is_reused_without_retry_or_api(
    runner, tmp_path
):
    key, repetition = "context-hash", 0
    folder = tmp_path / "judge/production"
    folder.mkdir(parents=True)
    judgment = runner.Judgment(
        readings=[],
        checks=[
            {
                "dimension": dimension,
                "verdict": "pass",
                "quotes": [],
                "reason": "확인됨",
            }
            for dimension in [
                "intent",
                "binding",
                "completeness",
                "grounding",
                "consistency",
            ]
        ],
    )
    runner.write(
        folder / f"{key}-r{repetition}-a0.json",
        {
            "key": key,
            "repetition": repetition,
            "attempt": 0,
            "raw_response": {
                "model": runner.MODEL,
                "choices": [
                    {"message": {"content": judgment.model_dump_json()}}
                ],
            },
        },
    )
    # Any access to a client would fail: this is a saved-record replay, not an API test.
    result = runner.judge_once(
        None,
        tmp_path,
        "production",
        key,
        repetition,
        {
            "answer": "확정할 수 없습니다.",
            "question": "미래 금리?",
            "reference": {"facts": []},
        },
    )
    assert result["numeric"] == "not_applicable"
    assert result["verdict"] == "pass"
    assert len(list(folder.glob("*.json"))) == 1


def test_nonretryable_saved_error_does_not_make_new_attempt(runner, tmp_path):
    folder = tmp_path / "judge/production"
    folder.mkdir(parents=True)
    runner.write(
        folder / "key-r0-a0.json",
        {"error": "UnexpectedModel", "retryable": False},
    )
    result = runner.judge_once(None, tmp_path, "production", "key", 0, {})
    assert result["verdict"] == "review"
    assert result["error"] == "UnexpectedModel"
    assert len(list(folder.glob("*.json"))) == 1
