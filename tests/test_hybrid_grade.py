"""Validate the hybrid evaluator's numeric boundary and uncertain judge output."""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "hybrid_grade", Path(__file__).resolve().parents[1] / "eval/hybrid_grade.py"
)
GRADE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GRADE)


@pytest.mark.parametrize(
    "number,unit,value,canonical",
    [
        ("1,089,648원", "원", "1089648", "KRW"),
        ("108만9천648원", "원", "1089648", "KRW"),
        ("1억 2,500만 원", "원", "125000000", "KRW"),
        ("1.2만원", "원", "12000", "KRW"),
        ("0.05%p", "%p", "0.05", "percentage_point"),
        ("3.2 %", "%", "3.2", "percent"),
        ("18개월", "개월", "18", "month"),
        ("10만원 이상", "원", "100000", "KRW"),
    ],
)
def test_parse_amount_and_units(number, unit, value, canonical):
    assert GRADE.parse_quantity(number, unit) == (Decimal(value), canonical)


@pytest.mark.parametrize(
    "number,unit",
    [
        ("삼만원", "원"),
        ("약 1만원", "원"),
        ("3.2%", "%p"),
        ("1e6", "원"),
        ("NaN", "원"),
        ("Infinity", "원"),
        ("3.2", "달러"),
    ],
)
def test_unsupported_or_conflicting_amount_is_not_guessed(number, unit):
    with pytest.raises(ValueError):
        GRADE.parse_quantity(number, unit)


def payload(answer="세후 이자는 1,089,648원입니다."):
    return {
        "answer": answer,
        "question": "세후 이자는 몇 원인가요?",
        "reference": {
            "facts": [
                {
                    "id": "Q20.f001",
                    "value": "1089648",
                    "unit": "KRW",
                    "required": True,
                    "condition": "세후; 각각 원 미만 버림",
                }
            ]
        },
    }


def judgment(
    *,
    state="asserted",
    quote="1,089,648원",
    number="1,089,648원",
    semantic="pass",
    include_reading=True,
):
    return GRADE.Judgment(
        readings=[
            GRADE.Reading(
                fact_id="Q20.f001",
                state=state,
                quote=quote,
                number_text=number,
                unit_text="원",
                unit_source="answer",
                reason="질문의 세후 이자 결과",
            )
        ]
        if include_reading
        else [],
        checks=[
            GRADE.SemanticCheck(
                dimension=dimension,
                verdict=semantic,
                quotes=[],
                reason="질문과 답변의 연결을 확인했다.",
            )
            for dimension in [
                "intent",
                "binding",
                "completeness",
                "grounding",
                "consistency",
            ]
        ],
    )


def test_exact_one_won_error_fails_despite_semantic_pass():
    result = GRADE.evaluate(
        payload("세후 이자는 1,089,647원입니다."),
        judgment(quote="1,089,647원", number="1,089,647원"),
    )
    assert result["numeric"] == result["verdict"] == "fail"
    assert result["semantic"] == "pass"
    assert result["numeric_checks"][0]["actual_value"] == "1089647"


def test_wrong_quote_or_fabricated_number_requires_review():
    wrong_quote = GRADE.evaluate(
        payload(), judgment(quote="정답은 1,089,648원")
    )
    assert wrong_quote["verdict"] == "review"
    fabricated_number = GRADE.evaluate(payload(), judgment(number="999999원"))
    assert fabricated_number["verdict"] == "review"
    wrong_semantic_quote = judgment()
    wrong_semantic_quote.checks[0].quotes = ["답변에 존재하지 않는 문장"]
    assert (
        GRADE.evaluate(payload(), wrong_semantic_quote)["verdict"] == "review"
    )


def test_missing_required_answer_fails_but_missing_judge_reading_requires_review():
    missing = GRADE.evaluate(
        payload("계산할 수 없습니다."),
        judgment(state="missing", quote="", number=""),
    )
    assert missing["numeric"] == missing["verdict"] == "fail"
    omitted = GRADE.evaluate(payload(), judgment(include_reading=False))
    assert omitted["numeric"] == omitted["verdict"] == "review"


def test_unsupported_number_requires_review_instead_of_numeric_failure():
    result = GRADE.evaluate(
        payload("세후 이자는 백만원입니다."),
        judgment(quote="백만원", number="백만원"),
    )
    assert result["numeric"] == result["verdict"] == "review"


def test_numeric_failure_cannot_be_overridden_by_semantic_pass_or_consensus():
    result = GRADE.evaluate(
        payload("세후 이자는 1,089,647원입니다."),
        judgment(quote="1,089,647원", number="1,089,647원"),
    )
    assert GRADE.aggregate([result, result])["verdict"] == "fail"
    assert not GRADE.aggregate([result, result])["verdict_disagreement"]


def test_semantic_failure_cannot_be_overridden_by_correct_number():
    result = GRADE.evaluate(payload(), judgment(semantic="fail"))
    assert result["numeric"] == "pass"
    assert result["semantic"] == result["verdict"] == "fail"


def test_two_independent_judges_disagreement_requires_review():
    passing = GRADE.evaluate(payload(), judgment())
    failing = GRADE.evaluate(payload(), judgment(semantic="fail"))
    result = GRADE.aggregate([passing, failing])
    assert result["numeric"] == "pass"
    assert result["semantic"] == result["verdict"] == "review"
    assert result["disagreement"] and result["verdict_disagreement"]
    with pytest.raises(ValueError):
        GRADE.aggregate([passing])


def test_duplicate_semantic_dimension_and_unknown_fact_require_review():
    duplicate = judgment()
    duplicate.checks[-1].dimension = "intent"
    assert GRADE.evaluate(payload(), duplicate)["verdict"] == "review"
    unknown = judgment()
    unknown.readings[0].fact_id = "unknown"
    assert GRADE.evaluate(payload(), unknown)["verdict"] == "review"


@pytest.mark.parametrize(
    "number,expected",
    [("-1만2천원", "-12000"), ("−1억2천만원", None), ("+1만2천원", "12000")],
)
def test_signed_mixed_korean_amounts(number, expected):
    if expected is None:
        with pytest.raises(ValueError):
            GRADE.parse_quantity(number, "원")
    else:
        assert GRADE.parse_quantity(number, "원") == (Decimal(expected), "KRW")


def test_ascending_or_repeated_korean_scales_require_review():
    for number in ["1천2만원", "1만2만원", "1만-2천원"]:
        with pytest.raises(ValueError):
            GRADE.parse_quantity(number, "원")


def test_status_agreement_ignores_different_judge_wording():
    first = GRADE.evaluate(payload(), judgment())
    second_judge = judgment()
    second_judge.checks[0].reason = "별도의 독립 평가 설명"
    second = GRADE.evaluate(payload(), second_judge)
    combined = GRADE.aggregate([first, second])
    assert combined["verdict"] == "pass"
    assert not combined["disagreement"]


def test_proven_numeric_failure_remains_fail_with_malformed_semantic_check():
    verdict = judgment(quote="1,089,647원", number="1,089,647원")
    verdict.checks[0].quotes = ["원문에 없는 인용"]
    result = GRADE.evaluate(payload("세후 이자는 1,089,647원입니다."), verdict)
    assert result["numeric"] == result["verdict"] == "fail"
    assert result["semantic"] == "review"


def test_schema_restricts_ids_and_empty_reference_readings():
    schema = GRADE.judgment_schema(payload()["reference"])
    assert "arithmetic" in schema["required"]
    assert "default" not in schema["properties"]["arithmetic"]
    assert schema["$defs"]["Reading"]["properties"]["fact_id"]["enum"] == [
        "Q20.f001"
    ]
    empty = GRADE.judgment_schema({"facts": []})
    assert empty["properties"]["readings"]["maxItems"] == 0
    assert "maxItems" not in schema["properties"]["readings"]


def test_annual_prefix_and_literal_unit_take_precedence_over_extraction_label():
    assert GRADE.parse_quantity("연 3.6%", "%") == (Decimal("3.6"), "percent")
    j = judgment()
    j.readings[0].unit_text = "KRW"
    result = GRADE.evaluate(payload(), j)
    assert result["verdict"] == "pass"
    assert result["numeric_checks"][0]["resolved_unit_text"] == "원"


def test_valid_semantic_failure_does_not_require_parseable_numbers():
    j = judgment()
    j.readings[0].state = "ambiguous"
    j.checks[0].verdict = "fail"
    j.checks[0].reason = "다른 상품에 답함"
    result = GRADE.evaluate(payload(), j)
    assert result["numeric"] == "review"
    assert result["semantic"] == result["verdict"] == "fail"


@pytest.mark.parametrize(
    "answer,quote,number,unit,expected",
    [
        ("13.2%", "13.2%", "3.2%", "%", "3.2"),
        ("13.2%", "3.2%", "3.2%", "%", "3.2"),
        ("1,000원", "1,000원", "000원", "원", "0"),
        ("23,000,000원", "23,000,000원", "3,000,000", "원", "3000000"),
        ("-3.2%", "-3.2%", "3.2%", "%", "3.2"),
        ("3.25%", "3.25%", "3.2", "%", "3.2"),
        ("1만2천원", "1만2천원", "2천원", "원", "2000"),
        ("3.2%p", "3.2%p", "3.2%", "%", "3.2"),
    ],
)
def test_truncated_numeric_token_requires_review(
    answer, quote, number, unit, expected
):
    data = payload(answer)
    fact = data["reference"]["facts"][0]
    fact.update(value=expected, unit="percent" if unit == "%" else "KRW")
    j = judgment(quote=quote, number=number)
    j.readings[0].unit_text = unit
    result = GRADE.evaluate(data, j)
    assert result["numeric"] == result["verdict"] == "review"


@pytest.mark.parametrize(
    "answer,number,unit,expected",
    [
        ("연 3.2%", "3.2%", "%", "3.2"),
        ("월 3.2%", "월 3.2%", "%", "3.2"),
        ("금리 3.2 %", "3.2", "%", "3.2"),
        ("1,000원,", "1,000원", "원", "1000"),
        ("1,000 원", "1,000", "원", "1000"),
        ("최소 원금은 11,531,721원입니다.", "11531721", "원", "11531721"),
        ("연 3.2 %", "3.2%", "%", "3.2"),
    ],
)
def test_complete_numeric_token_preserves_prefix_and_separate_unit(
    answer, number, unit, expected
):
    data = payload(answer)
    fact = data["reference"]["facts"][0]
    fact.update(value=expected, unit="percent" if unit == "%" else "KRW")
    j = judgment(quote=answer, number=number)
    j.readings[0].unit_text = unit
    result = GRADE.evaluate(data, j)
    assert result["numeric"] == result["verdict"] == "pass"


@pytest.mark.parametrize("label", ["KRW", "month"])
def test_unit_label_is_recovered_only_from_adjacent_original_unit(label):
    j = judgment(number="1,089,648")
    j.readings[0].unit_text = label
    result = GRADE.evaluate(payload(), j)
    assert result["verdict"] == "pass"
    assert result["numeric_checks"][0]["resolved_unit_text"] == "원"
    no_original_unit = GRADE.evaluate(
        payload("결과는 1,089,648입니다."),
        judgment(quote="1,089,648", number="1,089,648"),
    )
    assert no_original_unit["verdict"] == "review"
    assert no_original_unit["extraction_errors"][0]["component"] == "numeric"


def test_ellipsis_recovers_unique_original_span_and_preserves_audit_trail():
    data = payload("우리SUPER주거래적금 24개월 자유적립식 단리의 기본금리는 2.4%입니다.")
    data["reference"]["facts"][0].update(value="2.4", unit="percent")
    j = judgment(quote=data["answer"], number="2.4%")
    j.checks[0].quotes = ["우리SUPER주거래적금 24개월 ... 기본금리는 2.4%입니다."]
    result = GRADE.evaluate(data, j)
    assert result["verdict"] == "pass"
    assert result["semantic_checks"][0]["quotes"] == [data["answer"]]
    assert "original_quotes" in result["semantic_checks"][0]
    j.checks[0].quotes = ["우리SUPER주거래적금 ... 최고금리는 2.4%입니다."]
    assert GRADE.evaluate(data, j)["verdict"] == "review"


def test_ambiguous_ellipsis_is_not_silently_repaired():
    assert GRADE.resolve_quote("금리 1%입니다. 금리 2%입니다.", "금리 ... 입니다.") is None


@pytest.mark.parametrize("amount,expected", [("12.5", "fail"), ("12", "pass")])
def test_half_relation_checks_false_explanation_despite_correct_final_value(amount, expected):
    text = f"평균 기간은 {amount}개월(24개월의 절반)입니다. 세후 이자는 1,089,648원입니다."
    j = judgment()
    j.arithmetic = [GRADE.ArithmeticRelation(
        quote=f"{amount}개월(24개월의 절반)", operation="half",
        left_text="24개월", right_text="", result_text=f"{amount}개월",
    )]
    result = GRADE.evaluate(payload(text), j)
    assert result["numeric"] == result["semantic"] == "pass"
    assert result["arithmetic"] == result["verdict"] == expected
    assert result["arithmetic_checks"][0]["expected_value"] == "12"


def test_arithmetic_requires_original_quote_and_complete_operands():
    relation = GRADE.ArithmeticRelation(
        quote="12.5개월(24개월의 절반)", operation="half",
        left_text="24개월", right_text="", result_text="12.5개월",
    )
    assert GRADE.evaluate_arithmetic("원문에는 이 관계가 없습니다.", relation)["verdict"] == "review"
    relation.left_text = "4개월"
    assert GRADE.evaluate_arithmetic(relation.quote, relation)["verdict"] == "review"


@pytest.mark.parametrize("quote,op,left,right,result,expected", [
    ("2원 + 3원 = 5원", "add", "2원", "3원", "5원", "pass"),
    ("24 - 12 = 12", "subtract", "24", "12", "12", "pass"),
    ("24 / 2 = 12", "divide", "24", "2", "12", "pass"),
    ("24 / 0 = 12", "divide", "24", "0", "12", "review"),
    ("2원 × 3 = 6원", "multiply", "2원", "3", "6원", "pass"),
    ("24개월의 절반은 12원", "half", "24개월", "", "12원", "review"),
    ("복잡한 관계", "unsupported", "", "", "", "review"),
])
def test_supported_arithmetic_and_uncertain_relationships(quote, op, left, right, result, expected):
    relation = GRADE.ArithmeticRelation(quote=quote, operation=op, left_text=left, right_text=right, result_text=result)
    assert GRADE.evaluate_arithmetic(quote, relation)["verdict"] == expected


def test_optional_omission_passes_but_actual_wrong_optional_claim_fails():
    data = payload()
    optional = {**data["reference"]["facts"][0], "id": "optional", "required": False, "value": "100"}
    data["reference"]["facts"].append(optional)
    assert GRADE.evaluate(data, judgment())["verdict"] == "pass"
    j = judgment()
    j.readings.append(GRADE.Reading(fact_id="optional", state="asserted", quote="99원", number_text="99원", unit_text="원", unit_source="answer", reason="추가 금액 주장"))
    data["answer"] += " 추가 금액은 99원입니다."
    assert GRADE.evaluate(data, j)["verdict"] == "fail"
