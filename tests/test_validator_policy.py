"""검증기 방향: 핵심 금융 사실(대상·조건·항목·수치)은 엄격하게, 표현·배치·의역은 허용.
평가 오답 원인(2026-10-21 측정)의 회귀도 함께 묶는다."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_graph import CAT, FakeLLM, ans, run

from finagent import catalog as C
from finagent.catalog import Catalog
from finagent.deps import Deps
from finagent.graph.nodes import (
    FUTURE_REPLY,
    finalize_node,
    product_doc,
    refuse_node,
)
from finagent.parser import parse_all
from finagent.provenance import check_provenance
from finagent.requirements import check_membership_claims, check_requirements
from finagent.schemas import (
    AnswerInput,
    Calculation,
    Description,
    ProductDoc,
    RateRow,
    RequiredFact,
)
from finagent.slots import extract, route_rules
from finagent.validate import (
    allowed_values,
    check_answer,
    check_equations,
    condition_rate,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def real():
    """실제 공시 카탈로그(데이터가 없으면 skip)."""
    if not any((ROOT / "data/raw").glob("*.json")):
        pytest.skip("공시 데이터 없음")
    return Catalog(parse_all(ROOT / "data/raw"))


# ---------------- 슬롯·라우팅 ----------------
@pytest.mark.parametrize(
    "q,group",
    [
        ("저축은행은 제외하고 은행 정기예금 중 6개월 기본금리 1위", "bank"),
        ("저축은행 빼고 은행 적금 추천", "bank"),
        ("은행 말고 저축은행 예금 금리 높은 곳", "savingsbank"),
        ("저축은행 적금 금리 높은 곳", "savingsbank"),
        ("은행 정기예금 금리 높은 곳", "bank"),
    ],
)
def test_excluded_financial_group_is_not_selected(q, group):
    """'저축은행은 제외'를 저축은행 조회로 읽지 않는다(평가 Q13~18)."""
    assert extract(q, CAT).group == group


def test_excluding_every_group_fails_loudly():
    """두 금융권을 모두 빼면 조용히 전체 조회하지 않고 실패한다."""
    with pytest.raises(ValueError, match="조회할 금융권이 없습니다"):
        extract("은행 말고 저축은행도 빼고 예금 금리 알려줘", CAT)


def test_excluded_item_is_not_read_as_the_asked_metric():
    """'전체 우대 폭은 빼고'는 우대 폭 질문이 아니다(평가 Q27~29)."""
    s = extract(
        "가나적금의 급여 이체 조건 개별 우대금리는 몇 %p인가요? "
        "기본금리나 전체 우대 폭은 빼고 해당 조건만 설명해주세요.",
        CAT,
    )
    assert s.metric != "spread"
    assert extract("가나적금 우대 폭 얼마야?", CAT).metric == "spread"


@pytest.mark.parametrize(
    "q,future",
    [
        (
            "내년 이맘때 우리은행 예금 금리가 정확히 몇 %인지 확정해서 알려줘.",
            True,
        ),
        ("3년 후 적금 금리가 몇 %가 될지 예측해줘", True),
        ("1년 뒤 만기 이자 얼마야? 월 10만원 적금 3%", False),
        ("12개월 뒤 받을 이자가 얼마가 될까? 예금 1000만원 3%", False),
    ],
)
def test_future_rate_certainty_is_answered_without_a_rate(q, future):
    """미래 금리를 확정해 달라는 질문은 금리 없이 안내한다(평가 Q38)."""
    route, _ = route_rules(q, extract(q, CAT))
    assert (route == "out_of_scope") is future
    if future:
        deps = SimpleNamespace(data_month="202609")
        final = asyncio.run(refuse_node({"query": q, "route": route}, deps))[
            "final"
        ]
        assert final["reply"] == FUTURE_REPLY and "%" not in final["reply"]


def test_insufficient_reply_is_a_complete_sentence():
    """근거 부족 문구는 LLM의 missing을 이어 붙여도 문장이 끝난다(평가 Q39)."""
    state = {
        "route": "easy",
        "insufficient": True,
        "draft": {"missing": "777개월 기간의 기본금리 정보"},
        "tasks": [],
        "results": [],
    }
    final = asyncio.run(
        finalize_node(state, SimpleNamespace(data_month="202609"))
    )["final"]
    assert final["reply"] == (
        "공시 자료만으로는 답하기 어려워요. "
        "확인하지 못한 내용: 777개월 기간의 기본금리 정보."
    )


# ---------------- 계산 ----------------
def test_interest_uses_decimal_not_float_truncation():
    """float 절사로 1원이 모자라던 계산을 Decimal로 정확히 한다(평가 Q20)."""
    pre = C.deposit_interest(23_000_000, 24, 2.8)
    assert pre == 1_288_000 and C.after_tax(pre) == 1_089_648
    assert C.deposit_interest(17_000_000, 18, 3.2) == 816_000
    assert C.deposit_goal(12_000_000, 18, 3.2) == 11_531_721
    assert C.saving_goal(12_000_000, 18, 3.2) == 652_679
    assert C.loan_payment(73_000_000, 180, 4.2, "bullet") == 255_500


@pytest.mark.parametrize(
    "reply,wrong",
    [
        ("이자 = 17,000,000 × 0.032 × 1.5 = 816,000원", False),
        (
            "월 이자 = (원금) × (연 이자율) ÷ 12\n = 73,000,000원 × 0.042 ÷ 12\n = 255,500원",
            False,
        ),
        ("평균 기간은 (24+1)÷2=12.5개월입니다.", False),
        ("평균 기간은 24÷2=12.5개월입니다.", True),
        ("2.45% + 0.7%p = 3.15%입니다.", False),
        ("2.45% + 0.7%p = 3.85%입니다.", True),
        ("기본금리 2.45%에 우대 0.7%p를 더하면 3.15%입니다.", False),
        ("기본금리 2.45%에 우대 0.7%p를 더하면 3.85%입니다.", True),
    ],
)
def test_written_arithmetic_is_recomputed(reply, wrong):
    """계산 과정은 생략해도 되지만, 쓴 등식·합산이 틀리면 확인된 오류다."""
    errors, _ = check_equations(reply)
    assert bool(errors) is wrong


def test_correct_equation_result_is_allowed_number():
    """맞는 등식의 결과는 조회·계산 결과에 없어도 근거 있는 수치로 본다."""
    reply = "총 납입액 = 650,000원 × 24 = 15,600,000원입니다."
    allowed = {"pct": set(), "won": {650000.0}}
    assert check_answer(reply, allowed, [], {}) == (True, None)
    ok, why = check_answer(
        reply.replace("15,600,000", "15,700,000"), allowed, [], {}
    )
    assert not ok and "15,700,000원" in why


# ---------------- 개별 우대조건 ----------------
def _saving_doc():
    """우대조건 줄이 여러 개인 가짜 적금 문서."""
    return ProductDoc(
        product_id="a",
        title="가은행 가나적금",
        info={"상품명": "가나적금", "금융회사": "가은행"},
        rate_table="",
        rates=[
            RateRow(
                product_id="a",
                bank="가은행",
                product="가나적금",
                option="12개월 단리",
                metric="우대 폭(최고-기본)",
                value=1.1,
                unit="%p",
            )
        ],
        descriptions=[
            Description(
                section="우대조건",
                text="가.급여/연금 이체:연 0.7%p\n"
                "나.공과금 자동이체 출금: 0.3%p\n"
                "다.마케팅 동의 후 만기까지 유지 : 연 0.1%p",
            )
        ],
    )


@pytest.mark.parametrize(
    "q,value",
    [
        (
            "가나적금 공과금 자동이체 조건 개별 우대금리는 몇 %p? 전체 우대 폭은 빼고",
            0.3,
        ),
        ("월급 이체하면 가나적금 우대 얼마나 더 붙어?", 0.7),
        ("가나적금 마케팅 동의하면 추가 우대 있어?", 0.1),
    ],
)
def test_condition_rate_finds_any_disclosed_condition(q, value):
    """급여·신규고객만이 아니라 공시된 어느 우대조건이든 그 줄의 우대율을 찾는다(평가 Q27~29)."""
    label, vals = condition_rate(q, [_saving_doc()])
    assert vals == {value} and "조건 우대금리" in label


def test_condition_rate_does_not_guess_when_nothing_matches():
    """조건 단어가 겹치지 않거나 전체 금리를 물으면 확정하지 않는다."""
    doc = _saving_doc()
    assert condition_rate("가나적금 우대조건 알려줘", [doc]) is None
    assert condition_rate("가나적금 급여 이체하면 최고금리는?", [doc]) is None


def _condition_input(question):
    """개별 우대조건 질문의 답변 근거."""
    return AnswerInput(question=question, documents=[_saving_doc()])


@pytest.mark.parametrize(
    "reply",
    [
        "가나적금 공과금 자동이체 우대는 0.3%p입니다.",
        # 의역·목록 배치: 원문 줄을 그대로 복사하지 않아도 조건과 우대율이 맞으면 통과
        "가나적금 우대조건\n- 급여나 연금을 이체하면 0.7%p\n- 공과금을 자동이체하면 0.3%p\n- 마케팅 동의 유지 시 0.1%p",
    ],
)
def test_paraphrased_condition_with_matching_rate_passes(reply):
    """조건·우대율이 맞으면 표현은 자유롭다(평가 Q30)."""
    inp = _condition_input("가나적금 우대조건 알려줘")
    notes = []
    assert not check_provenance(inp, reply, notes)
    assert not notes


def test_condition_with_another_conditions_rate_is_a_confirmed_error():
    """조건과 우대율을 엇갈리게 쓰면 숫자가 공시에 있어도 확인된 오류다."""
    inp = _condition_input("가나적금 공과금 자동이체 우대 얼마야?")
    errors = check_provenance(
        inp, "가나적금 공과금 자동이체 우대는 0.7%p입니다."
    )
    assert errors and "우대조건과 우대율 불일치" in errors[0]


def test_unlinked_number_is_a_note_not_a_rejection():
    """상품에 연결하지 못한 수치는 반려하지 않고 '검증 못 함'으로 남긴다."""
    inp = _condition_input("가나적금 우대조건 알려줘")
    inp.documents.append(
        ProductDoc(
            product_id="b",
            title="다은행 다라적금",
            info={"상품명": "다라적금", "금융회사": "다은행"},
            rate_table="",
            rates=[],
            descriptions=[
                Description(section="우대조건", text="급여 이체: 연 0.5%p")
            ],
        )
    )
    notes = []
    assert not check_provenance(inp, "급여 이체 우대는 0.5%p입니다.", notes)
    assert notes == ["근거와 연결하지 못한 수치: 0.5%p"]


# ---------------- 역산 ----------------
@pytest.fixture
def goal_input():
    """세후 만기 1,200만원 목표 예금 원금 역산 근거(평가 Q25)."""
    return AnswerInput(
        question="연 3.2% 단리, 18개월, 이자 세율 15.4%로 세후 만기 수령액 "
        "1,200만원 이상을 만들려면 최소 예금 원금은 몇 원인가요?",
        calculations=[
            Calculation(
                name="목표 달성 필요 원금",
                result=11_531_721,
                inputs={
                    "product_type": "deposit",
                    "amount_won": 12_000_000,
                    "term_months": 18,
                    "rate_pct": 3.2,
                    "mode": "goal",
                    "interest_type": "simple",
                },
                formula="원금 + 세후이자 ≥ 목표 금액인 최소 금액",
            )
        ],
    )


def test_reverse_calculation_answer_is_accepted(goal_input):
    """역산 결과는 원금·월 납입액으로, 목표는 만기 수령액으로 대조한다(평가 Q25·Q26)."""
    reply = (
        "연 3.2% 단리, 18개월, 세율 15.4%로 세후 만기 수령액 1,200만원 이상을 "
        "만들려면 최소 예금 원금은 11,531,721원입니다."
    )
    assert check_answer(
        reply,
        allowed_values(goal_input, goal_input.question),
        [],
        {},
        required=[(11_531_721,)],
        question=goal_input.question,
    ) == (True, None)
    assert not check_provenance(goal_input, reply)


def test_reverse_calculation_roles_cannot_be_swapped(goal_input):
    """목표 금액을 필요 원금이라고 하면 확인된 오류다."""
    errors = check_provenance(
        goal_input, "최소 예금 원금은 12,000,000원입니다."
    )
    assert errors and "12,000,000원" in errors[0]


# ---------------- 가입 정보 ----------------
def _member_input(deny):
    """가입 제한 공시값만 다른 가입정보 근거."""
    return AnswerInput(
        question="가나적금 가입 제한 알려줘",
        documents=[
            ProductDoc(
                product_id="a",
                title="가은행 가나적금",
                info={
                    "상품명": "가나적금",
                    "금융회사": "가은행",
                    "가입 제한": deny,
                },
                rate_table="",
                rates=[],
                descriptions=[],
            )
        ],
    )


def test_membership_paraphrase_is_a_note_but_removing_a_restriction_is_an_error():
    """공시와 같은 뜻의 의역은 경고만, 공시된 제한을 없애는 주장은 확인된 오류다."""
    notes = []
    assert not check_membership_claims(
        _member_input("제한없음"), "가나적금은 가입 제한이 없습니다.", notes
    )
    assert notes
    assert check_membership_claims(
        _member_input("서민전용"), "가나적금은 가입 제한이 없습니다."
    )
    assert check_membership_claims(
        _member_input("제한없음"),
        "가나적금은 누구나 조건 없이 가입할 수 있어요.",
    )


# ---------------- 그래프: 경고는 남기고 답은 살린다 ----------------
def test_unverified_note_keeps_the_answer_and_adds_a_warning():
    """해석하지 못한 수치만 있는 답은 반려하지 않고 경고를 붙여 그대로 낸다."""
    final, st = run(
        "은행 적금 중에 12개월 기본금리 제일 높은 거 뭐야?",
        FakeLLM(
            ans(
                "다라적금이 3.2%로 가장 높아요.\n\n참고로 2.45%인 상품도 있어요."
            )
        ),
    )
    assert st["verified"] and final["reply"].startswith("다라적금이 3.2%")
    assert st["unverified"] == ["근거와 연결하지 못한 수치: 2.45%"]
    assert any("연결하지 못한 수치·설명 1건" in w for w in final["warnings"])


def test_real_individual_condition_questions(real):
    """실제 공시에서 평가 Q27~29의 개별 우대율을 찾는다."""
    deps = Deps(None, None, None, real, "202609", "rule", "fixed")

    def doc(name):
        return product_doc(
            deps, next(p.id for p in real.products if p.name == name)
        )

    tail = "에 해당하는 개별 우대금리는 몇 %p인가요? 기본금리나 전체 우대 폭은 빼고 해당 조건만 설명해주세요."
    for q, name, value in [
        ("우리은행 WON적금의 타행계좌 오픈뱅킹 등록 조건", "WON적금", 0.1),
        (
            "아이엠뱅크 iM함께적금의 인터넷 또는 모바일 가입 조건",
            "iM함께적금",
            0.05,
        ),
        (
            "우리은행 우리SUPER주거래적금의 공과금 자동이체 조건",
            "우리SUPER주거래적금",
            0.3,
        ),
    ]:
        assert condition_rate(q + tail, [doc(name)])[1] == {value}


@pytest.mark.parametrize(
    "reply",
    [
        "1,000,000원 × 3% = 30,000원",
        "2.5% + 0.3%p = 2.8%",
        "24개월의 절반은 12.5개월 = 12.5/12년",
        "200,000 × 0.0020833 × 300 = 125,000원",
    ],
)
def test_equation_percent_unit_change_and_rounded_factor_are_accepted(reply):
    assert check_equations(reply)[0] == []


def test_equation_rounded_factor_still_catches_real_error():
    assert check_equations("200,000 × 0.0020833 × 300 = 130,000원")[0]


def test_after_tax_fact_checks_every_mention():
    inp = AnswerInput(
        question="세후 이자 얼마?",
        required_facts=[
            RequiredFact(section="세후 이자", kind="won", value="84600")
        ],
    )
    reply = (
        "세후라는 말은 이자소득세 15.4%를 원천징수한 뒤 실제로 받는 금액이라는 뜻이고, "
        "만기일에 계좌로 들어오는 돈을 기준으로 설명합니다. 세후 이자는 84,600원입니다."
    )
    assert not [e for e in check_requirements(inp, reply) if "세후" in e]
