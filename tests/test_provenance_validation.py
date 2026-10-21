"""검증기가 받은 초안을 오염시켜 실제 공시·계산 근거와 대조한다."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from finagent.catalog import Catalog
from finagent.graph.nodes import product_doc
from finagent.parser import parse_all
from finagent.provenance import check_provenance
from finagent.schemas import (
    AnswerInput,
    Calculation,
    Description,
    ProductDoc,
    RateRow,
)
from finagent.validate import allowed_values, check_answer


def row(
    pid="a",
    product="가나적금",
    option="12개월 단리",
    metric="기본금리",
    value=3.75,
    unit="%",
):
    return RateRow(
        product_id=pid,
        bank="가은행",
        product=product,
        option=option,
        metric=metric,
        value=value,
        unit=unit,
    )


@pytest.fixture
def evidence():
    base = row()
    return AnswerInput(
        question="가나적금 12개월 기본금리와 최고금리 알려줘",
        rows=[base],
        documents=[
            ProductDoc(
                product_id="a",
                title="가은행 가나적금",
                info={"상품명": "가나적금", "금융회사": "가은행"},
                rate_table="",
                rates=[
                    base,
                    row(metric="최고금리(우대 포함)", value=3.95),
                    row(metric="우대 폭(최고-기본)", value=0.2, unit="%p"),
                    row(option="24개월 단리", value=4.0),
                ],
                descriptions=[
                    Description(
                        section="우대조건", text="급여 이체 시 연 0.2%p"
                    ),
                    Description(
                        section="가입 한도",
                        text="월 납입 한도는 100만원입니다.",
                    ),
                ],
            )
        ],
    )


@pytest.mark.parametrize(
    "reply",
    [
        "가나적금 12개월 기본금리 3.75%, 최고금리 3.95%입니다.",
        "가나적금은 12개월 기본금리 3.75퍼센트, 우대 폭 0.2퍼센트 포인트입니다.",
        "가나적금은 급여 이체 시 연 0.2%p",
        "가나적금: 월 납입 한도는 100만원입니다.",
    ],
)
def test_accepts_bound_rates_and_verbatim_source(evidence, reply):
    assert not check_provenance(evidence, reply)


@pytest.mark.parametrize(
    "reply",
    [
        "가나적금 12개월 기본금리 3.75%, 최고금리도 3.75%입니다.",
        "가나적금 24개월 기본금리 3.75%입니다.",
        "가나적금 12개월 복리 기본금리 3.75%입니다.",
        "가나적금 기본금리 3.75%p입니다.",
        "가나적금 우대 폭은 0.2%입니다.",
        "가나적금 12개월 기본금리 4%입니다.",
        "가나적금 만기 수령액은 100만원입니다.",
        "가나적금 급여 이체 시 연 0.2%p가 아닙니다.",
    ],
)
def test_rejects_wrong_metric_term_option_unit_and_amount_role(evidence, reply):
    assert check_provenance(evidence, reply)


def test_user_rate_is_not_evidence_but_a_grounded_denial_is_allowed(evidence):
    evidence.question = "가나적금 12개월 금리가 99%라던데 맞아?"
    allowed = allowed_values(evidence, evidence.question)
    assert 99 not in allowed["pct"]
    wrong = "가나적금 금리는 99%입니다."
    assert not check_answer(wrong, allowed, [], {}, question=evidence.question)[
        0
    ]
    assert check_provenance(evidence, wrong)
    corrected = "가나적금 금리는 99%가 아닙니다. 12개월 기본금리는 3.75%입니다."
    assert check_answer(corrected, allowed, [], {}, question=evidence.question)[
        0
    ]
    assert not check_provenance(evidence, corrected)
    assert check_provenance(evidence, corrected + " 따라서 99%입니다.")
    evidence.question = "가나적금 금리가 3.75%야?"
    assert check_provenance(evidence, "가나적금 기본금리는 3.75%가 아닙니다.")


def test_document_rates_are_scoped_without_structured_query(evidence):
    evidence.rows = []
    assert check_provenance(evidence, "가나적금 12개월 기본금리 4%입니다.")
    assert not check_provenance(
        evidence, "가나적금 12개월 기본금리 3.75%입니다."
    )
    assert check_provenance(evidence, "가나적금 기본금리 4%입니다.")


def test_one_products_rate_cannot_be_assigned_to_another(evidence):
    second = row(pid="b", product="다라적금", value=4.5)
    evidence.rows.append(second)
    evidence.documents.append(
        ProductDoc(
            product_id="b",
            title="다라적금",
            info={"상품명": "다라적금"},
            rate_table="",
            rates=[second],
        )
    )
    assert check_provenance(
        evidence, "가나적금 기본금리 4.5%, 다라적금 기본금리 3.75%입니다."
    )
    assert not check_provenance(
        evidence, "가나적금 기본금리 3.75%, 다라적금 기본금리 4.5%입니다."
    )
    # 상품을 밝히지 않은 수치는 오류로 확정하지 않고 '연결 못 함'으로만 남긴다.
    notes = []
    assert not check_provenance(evidence, "기본금리 4.5%입니다.", notes)
    assert notes == ["근거와 연결하지 못한 수치: 4.5%"]
    assert check_provenance(
        evidence, "가나적금과 다라적금은 기본금리 4.5%입니다."
    )


def test_calculation_premise_result_tax_and_product_are_bound(evidence):
    evidence.calculations = [
        Calculation(
            name="적금 이자",
            result=48750,
            after_tax=41242,
            inputs={
                "mode": "forward",
                "product_type": "saving",
                "amount_won": 200000,
                "term_months": 12,
                "rate_pct": 3.75,
            },
            formula="test",
            rate_from="가은행 가나적금 12개월 단리",
        )
    ]
    reply = "가나적금에 매월 20만원, 12개월 연 3.75%로 계산하면 세전 이자 48,750원, 세후 이자 41,242원입니다."
    assert not check_provenance(evidence, reply)
    assert check_provenance(
        evidence, reply.replace("세후 이자 41,242원", "세후 이자 48,750원")
    )
    assert check_provenance(evidence, "가나적금 원금은 48,750원입니다.")
    assert check_provenance(
        evidence, "가나적금에 12개월 복리 연 3.75%를 적용했습니다."
    )
    evidence.question += " 원금 99만원"
    assert 990000 not in allowed_values(evidence, evidence.question)["won"]


def test_rate_cannot_satisfy_small_amount_result():
    assert not check_answer(
        "연 3%입니다.", {"pct": {3.0}, "won": {3.0}}, [], {}, required=[(3,)]
    )[0]


@pytest.fixture(scope="module")
def actual_catalog():
    raw = Path(__file__).resolve().parents[1] / "data/raw"
    if not any(raw.glob("*.json")):
        pytest.skip("공시 원본 없음")
    return Catalog(parse_all(raw))


def test_original_corrupt_drafts_against_actual_disclosures(actual_catalog):
    p = next(
        p
        for p in actual_catalog.products
        if p.bank == "주식회사 카카오뱅크" and "자유적금" in p.name
    )
    inp = AnswerInput(
        question="카카오뱅크 자유적금 12개월 기본금리 알려줘",
        documents=[product_doc(SimpleNamespace(catalog=actual_catalog), p.id)],
    )
    inp.rows = [
        r
        for r in inp.documents[0].rates
        if r.option.startswith("12개월") and r.metric == "기본금리"
    ]
    assert inp.rows
    rate = inp.rows[0].value
    bad = f"카카오뱅크 자유적금 12개월 기본금리는 {rate}%, 우대 포함 최고금리도 {rate}%입니다."
    assert check_provenance(inp, bad)
    inp.question = "카카오뱅크 자유적금 금리가 99%라던데 맞아?"
    assert check_provenance(inp, "카카오뱅크 자유적금 금리는 99%입니다.")
    assert not check_provenance(
        inp,
        f"카카오뱅크 자유적금 금리는 99%가 아닙니다. 12개월 기본금리는 {rate}%입니다.",
    )


@pytest.fixture
def calculation_evidence(evidence):
    evidence.calculations = [
        Calculation(
            name="적금 이자",
            result=48750,
            after_tax=41242,
            inputs={
                "mode": "forward",
                "product_type": "saving",
                "amount_won": 200000,
                "term_months": 12,
                "rate_pct": 3.75,
            },
            formula="test",
            rate_from="가은행 가나적금 12개월 단리",
        )
    ]
    second = row(pid="b", product="다라적금", value=4.5)
    evidence.rows.append(second)
    evidence.documents.append(
        ProductDoc(
            product_id="b",
            title="다라적금",
            info={"상품명": "다라적금"},
            rate_table="",
            rates=[second],
        )
    )
    return evidence


@pytest.mark.parametrize(
    "reply",
    [
        "다라적금\n세전 이자 48,750원입니다.",
        "가나적금과 다라적금의 세전 이자는 48,750원입니다.",
        "가나적금 만기 수령액은 48,750원입니다.",
        "가나적금 세전 만기 수령액은 48,750원입니다.",
    ],
)
def test_rejects_wrong_calculation_product_and_final_balance(
    calculation_evidence, reply
):
    assert check_provenance(calculation_evidence, reply)


def test_calculation_heading_survives_one_newline_but_not_blank_line(
    calculation_evidence,
):
    assert not check_provenance(
        calculation_evidence, "가나적금\n세전 이자 48,750원입니다."
    )
    assert not check_provenance(
        calculation_evidence,
        "가나적금 기본금리는 3.75%입니다.\n다라적금 기본금리는 4.5%입니다.\n\n세전 이자는 48,750원입니다.",
    )


def test_assumed_rate_cannot_become_a_disclosed_product_rate(
    calculation_evidence,
):
    calculation_evidence.calculations[0].rate_from = None
    calculation_evidence.calculations[0].inputs["rate_pct"] = 99
    assert check_provenance(calculation_evidence, "가나적금 금리는 99%입니다.")
    assert not check_provenance(
        calculation_evidence, "가나적금에 연 99%를 가정해 계산합니다."
    )
    assert not check_provenance(
        calculation_evidence, "연 99%를 가정해 계산합니다."
    )


@pytest.mark.parametrize(
    "reply",
    [
        "최고금리: 가나적금 3.75%입니다.",
        "가나적금 기본금리는 급여 이체 시 연 0.2%p",
    ],
)
def test_preceding_metric_and_exact_quote_do_not_erase_claim_role(
    evidence, reply
):
    assert check_provenance(evidence, reply)


def test_markdown_tables_are_layout_not_errors():
    """표·목록 같은 배치는 검증하지 않고, 표 안의 수치만 근거와 대조한다."""
    assert check_answer(
        "| 상품 | 금리 |\n| --- | --- |\n| 가나 | 3% |",
        {"pct": {3}, "won": set()},
        [],
        {},
    )[0]
    assert not check_answer(
        "| 상품 | 금리 |\n| --- | --- |\n| 가나 | 9% |",
        {"pct": {3}, "won": set()},
        [],
        {},
    )[0]


def test_loan_calculation_term_does_not_filter_credit_score_rate_options():
    credit = row(
        product="가나대출", option="701~800점", metric="대출금리", value=6.19
    )
    inp = AnswerInput(
        question="신용점수 750점 신용대출 1억원 12개월 원리금균등 월상환액 계산",
        rows=[credit],
        calculations=[
            Calculation(
                name="대출 월 상환액",
                result=8615378,
                formula="test",
                rate_from="가은행 가나대출 701~800점",
                inputs={
                    "product_type": "loan",
                    "amount_won": 100000000,
                    "term_months": 12,
                    "rate_pct": 6.19,
                    "mode": "forward",
                    "repayment": "equal_payment",
                },
            )
        ],
    )
    assert not check_provenance(
        inp,
        "가나대출 701~800점 대출금리는 6.19%입니다.\n원리금균등 월 상환액은 8,615,378원입니다.",
    )
    inp.rows.append(
        row(
            pid="b",
            product="신용대출",
            option="701~800점",
            metric="대출금리",
            value=7.12,
        )
    )
    assert not check_provenance(
        inp,
        "신용점수 750점 신용대출 1억원 12개월 원리금균등 월 상환액은 가은행 가나대출 기준으로 8,615,378원입니다.",
    )
    assert check_provenance(inp, "가나대출 24개월 대출금리는 6.19%입니다.")
    assert check_provenance(inp, "가나대출 원리금 총액은 8,615,378원입니다.")


def test_bank_aliases_bind_only_unambiguous_products(evidence):
    evidence.rows[0].bank = "주식회사 가은행"
    assert not check_provenance(evidence, "가은행 기본금리는 3.75%입니다.")
    second = row(pid="b", product="다라적금", value=4.5)
    evidence.rows.append(second)
    assert check_provenance(evidence, "가은행 기본금리는 3.75%입니다.")
    assert not check_provenance(
        evidence, "가은행 가나적금 기본금리는 3.75%입니다."
    )


def test_calculation_bank_alias_does_not_weaken_product_binding(
    calculation_evidence,
):
    assert check_provenance(
        calculation_evidence, "가은행 다라적금 세전 이자는 48,750원입니다."
    )
    assert check_provenance(
        calculation_evidence, "가은행 세전 이자는 48,750원입니다."
    )
    assert not check_provenance(
        calculation_evidence, "가은행 가나적금 세전 이자는 48,750원입니다."
    )


def test_observed_fact_denial_is_narrow_and_still_requires_grounding(evidence):
    evidence.question = "가나적금 금리가 99%라던데 맞아?"
    reply = "가나적금 기본금리는 3.75%입니다. 99%라는 금리는 사실이 아닙니다."
    assert not check_provenance(evidence, reply)
    assert check_answer(
        reply,
        allowed_values(evidence, evidence.question),
        [],
        {},
        question=evidence.question,
    )[0]
    assert check_provenance(evidence, reply + " 따라서 99%입니다.")
    evidence.question = "가나적금 기본금리가 3.75%야?"
    assert check_provenance(
        evidence, "가나적금 기본금리 3.75%라는 금리는 사실이 아닙니다."
    )
