"""Reference generation is independent of agent output and legacy rubric values."""

import importlib.util
import json
import shutil
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hybrid_reference", ROOT / "eval/hybrid_reference.py"
)
REFERENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REFERENCE)
FROZEN = (
    ROOT
    / "eval/results/openai/gpt-4.1-mini-2025-04-14/20261021-fair-v2/snapshot"
)


def test_frozen_references_targets_ties_and_context():
    refs = REFERENCE.build_references(FROZEN)
    assert len(refs) == 40

    def required(key):
        return [f for f in refs[key]["facts"] if f["required"]]

    assert {f["product"] for f in required("Q15")} == {
        "코드K 자유적금",
        "카카오뱅크 자유적금",
    }
    assert all(
        f["term"] == 6 and f["unit"] == "percent" for f in required("Q15")
    )
    assert required("Q20")[0]["value"] == "1089648"
    assert required("Q23")[0]["value"] == "547318"
    assert required("Q24")[0]["value"] == "255500"
    assert required("Q25")[0]["value"] == "11531721"
    assert required("Q26")[0]["value"] == "652679"
    assert required("Q28")[0]["unit"] == "percentage_point"
    assert (
        len([f for f in required("Q30") if f["metric"] == "conditional_bonus"]) == 4
    )  # Both 0.3%p clauses remain independently scoped.
    assert required("Q31")[0]["product"] == "WON플러스예금"
    assert required("Q31")[0]["term"] == 24
    assert not refs["Q39"]["facts"]
    for key in ["Q25", "Q26"]:
        boundaries = [
            Decimal(f["value"])
            for f in refs[key]["facts"]
            if f["metric"] == "maturity"
        ]
        assert len(boundaries) == 2
        assert boundaries[0] < 12000000 <= boundaries[1]
    for ref in refs.values():
        assert ref["source_evidence"] and ref["semantic_requirements"]
        assert len({f["id"] for f in ref["facts"]}) == len(ref["facts"])
        for f in ref["facts"]:
            assert isinstance(f["value"], str)
            assert Decimal(f["value"]).is_finite()


def test_reference_ignores_legacy_rubric_and_embedded_source_answers(tmp_path):
    shutil.copytree(FROZEN / "data/raw", tmp_path / "data/raw")
    (tmp_path / "eval").mkdir()
    suite = [
        json.loads(line)
        for line in (FROZEN / "eval/fair_suite_20261021.jsonl")
        .read_text()
        .splitlines()
    ]
    for item in suite:
        item["rubric"] = {"value": -999, "winners": [], "terms": ["wrong"]}
        item["source"].pop("raw_options", None)
        item["source"].pop("boundary", None)
        item["source"].pop("text", None)
    (tmp_path / "eval/fair_suite_20261021.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in suite)
    )
    assert REFERENCE.build_references(tmp_path) == REFERENCE.build_references(
        FROZEN
    )


def test_calculation_background_covers_all_products_and_correct_limits():
    refs = REFERENCE.build_references(FROZEN)
    registry = next(
        json.loads(e)["product_registry"]
        for e in refs["Q22"]["source_evidence"]
        if "product_registry" in e
    )
    raw = json.loads(
        (FROZEN / "data/raw/savingProductsSearch_bank.json").read_text()
    )
    assert len(registry) == len(raw.get("result", raw)["baseList"])
    ibk = next(
        p for p in registry if p["productname"] == "IBK D-day적금(자유적립식)"
    )
    assert not ibk["has_requested_term"] and not ibk["options"]
    assert "3계좌" in ibk["etc_note"] and "20만원" in ibk["etc_note"]
    dream = next(p for p in registry if p["productname"] == "행복 DREAM 적금")
    assert "제한없음" in dream["etc_note"].replace(" ", "")
    assert any(
        o["intr_rate"] == 2.8 and o["save_trm"] == "24"
        for o in dream["options"]
    )


def test_new_validator_source_requires_no_audited_results_and_keeps_oracles():
    source = FROZEN.parents[0].parent / "20261022-validator"
    assert not (source / "baseline_audited.jsonl").exists()
    assert not (source / "current_audited.jsonl").exists()
    refs = REFERENCE.build_references(source)
    original = REFERENCE.build_references(FROZEN)
    assert len(refs) == 40
    for key in refs:
        assert refs[key]["facts"] == original[key]["facts"]


def test_ranking_background_covers_raw_simple_and_compound_without_promoting_them():
    refs = REFERENCE.build_references(FROZEN)
    for key in [f"Q{i}" for i in range(13, 19)]:
        source = next(json.loads(e) for e in refs[key]["source_evidence"]
                      if '"product_registry"' in e)
        data = json.loads((FROZEN / "data/raw" / source["file"]).read_text())
        data = data.get("result", data)
        assert len(source["product_registry"]) == len(data["baseList"])
        selection = source["selection"]
        assert selection["interest"] == "단리"
        assert selection["group"] == "bank"
        for entry in source["product_registry"]:
            expected = [o for o in data["optionList"]
                        if all(o[k] == entry["key"][k] for k in REFERENCE.KEY)
                        and int(o["save_trm"]) == selection["term"]]
            assert len(entry["options"]) == len(expected)
            assert entry["has_requested_term"] == bool(expected)
            assert {(o["intr_rate_type_nm"], o["intr_rate"], o["intr_rate2"])
                    for o in entry["options"]} == {
                        (o["intr_rate_type_nm"], o["intr_rate"], o["intr_rate2"])
                        for o in expected}
    q14 = next(json.loads(e) for e in refs["Q14"]["source_evidence"]
               if '"product_registry"' in e)
    assert any(o["intr_rate_type_nm"] == "복리"
               for p in q14["product_registry"] for o in p["options"])
    assert all("단리" in f["condition"] for f in refs["Q14"]["facts"])
    assert any("범위 위반" in r and "복리" in r
               for r in refs["Q14"]["semantic_requirements"])


def test_individual_bonus_scope_and_full_condition_scope_remain_distinct():
    refs = REFERENCE.build_references(FROZEN)
    q29 = refs["Q29"]
    required = [f for f in q29["facts"] if f["required"]]
    assert [(f["value"], f["unit"]) for f in required] == [("0.3", "percentage_point")]
    assert required[0]["condition"] == "공과금 자동이체 출금"
    assert any("전체의 설명은 필수가 아니" in r for r in q29["semantic_requirements"])
    assert any("조건을 덧붙인 경우에는 공시와 일치" in r
               for r in q29["semantic_requirements"])
    q30 = refs["Q30"]
    threshold = next(f for f in q30["facts"] if f["metric"] == "card_payment_threshold")
    assert threshold["required"] and threshold["value"] == "100000"
    requirements = q30["semantic_requirements"]
    assert any("1/2 이상" in r for r in requirements)
    assert any("모두 동의하고 만기까지 유지" in r for r in requirements)
    assert any("금리쿠폰" in r and "선택 사항" in r and "생략" in r for r in requirements)
