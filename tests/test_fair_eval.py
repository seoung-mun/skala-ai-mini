"""평가 도구 자체의 정답·채점 경계를 LLM 없이 검증한다."""

import importlib.util
import json
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1] / "eval"


def module(name):
    spec = importlib.util.spec_from_file_location(name, EVAL / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_suite_independent_oracles_and_context():
    suite = module("build_fair_suite").make_suite()
    assert len(suite) == len({i["id"] for i in suite}) == 40
    assert sum(bool(i["history"]) for i in suite) == 6
    assert all(i["source"] for i in suite)
    for item in suite:
        if item["category"] == "inverse":
            left, right = item["source"]["boundary"]
            assert left < 12000000 <= right
    # 원 단위 경계는 이진 float 구현에 맞춰 정답을 낮추지 않는다.
    decimal_case = next(
        i
        for i in suite
        if i["source"].get("inputs") == ["deposit", 23000000, 24, "2.8", True]
    )
    assert decimal_case["rubric"]["value"] == 1089648


def test_grade_rejects_wrong_units_conflicting_rates_and_negation():
    grade = module("run_fair_eval").grade
    item = {"rubric": {"type": "number", "value": 0.7, "unit": "%p"}}
    assert grade(item, "급여이체 개별 우대는 0.7%p입니다.")["correct"]
    assert not grade(item, "급여이체 개별 우대는 0.7%입니다.")["correct"]
    assert not grade(item, "0.7%p가 아니라 1.4%p입니다.")["correct"]
    assert not grade(item, "0.7%p 또는 1.4%p입니다.")["correct"]
    item = {"rubric": {"type": "number", "value": 822033, "unit": "원"}}
    assert grade(
        item, "목표 10,000,000원에 필요한 월 납입액은 822,033원입니다."
    )["correct"]


def test_grade_ties_and_clarification():
    grade = module("run_fair_eval").grade
    item = {
        "rubric": {
            "type": "ranking",
            "value": 3.8,
            "unit": "%",
            "winners": [{"name": "가적금"}, {"name": "나적금"}],
            "candidates": [
                {"name": "가적금"},
                {"name": "나적금"},
                {"name": "다적금"},
            ],
        }
    }
    assert grade(item, "가적금과 나적금이 공동 1위이며 3.8%입니다.")["correct"]
    assert not grade(item, "가적금이 3.8%로 1위입니다.")["correct"]
    assert not grade(item, "다적금이 1위이며 가적금과 나적금은 3.8%입니다.")[
        "correct"
    ]
    item = {"rubric": {"type": "clarify", "terms": ["점수"]}}
    assert grade(item, "신용점수가 필요합니다. 점수를 알려주세요.")["correct"]
    assert not grade(item, "점수는 모르지만 3.5%입니다.")["correct"]


def test_evaluator_revision_punctuation_and_valid_refusal():
    evaluator = module("run_fair_eval")
    assert evaluator.quantities("11,531,721원 (최소 금액, 원 단위)", "원") == [
        11531721
    ]
    item = {"rubric": {"type": "clarify", "terms": ["공시"]}}
    assert evaluator.grade(item, "공시 자료만으로는 답하기 어려워요.")[
        "correct"
    ]
    assert evaluator.grade(
        item, "죄송해요. 저는 공시된 금융상품 정보만 안내할 수 있어요."
    )["correct"]
    assert not evaluator.grade(item, "죄송해요. 공시에서 3.5%를 확인했어요.")[
        "correct"
    ]
    item = {"rubric": {"type": "clarify", "terms": ["미래"]}}
    assert evaluator.grade(
        item, "죄송하지만 내년 예금 금리는 참고 문서에 없습니다."
    )["correct"]


def test_audit_preserves_original_and_recovers_only_evaluator_error(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(EVAL))
    audit = module("audit_fair_eval")
    monkeypatch.setattr(audit, "report", lambda *args, **kwargs: None)
    src = tmp_path / "snapshot/eval"
    src.mkdir(parents=True)
    item = {"id": "X", "rubric": {"type": "number", "value": 12, "unit": "원"}}
    (src / "fair_suite_20261021.jsonl").write_text(json.dumps(item) + "\n")
    row = {
        "id": "X",
        "repeat": 0,
        "status": "error",
        "error_type": "ValueError",
        "correct": False,
        "answer": "",
        "observed_calls": [
            {"model": audit.MODEL, "usage": {"prompt_tokens": 1}}
        ],
        "final": {"reply": "필요 원금: 12원 (최소 금액, 원 단위)"},
    }
    original = json.dumps(row) + "\n"
    for system in ["baseline", "current"]:
        (tmp_path / f"{system}.jsonl").write_text(original)
    audit.audit(tmp_path)
    assert (tmp_path / "current.jsonl").read_text() == original
    fixed = json.loads((tmp_path / "current_audited.jsonl").read_text())
    assert fixed["correct"] and fixed["status"] == "ok"
    assert fixed["original_evaluation"]["correct"] is False
