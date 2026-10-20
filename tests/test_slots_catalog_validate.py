import json
from pathlib import Path

import pytest

from finagent.catalog import (Catalog, deposit_interest, loan_payment, saving_interest, score_band)
from finagent.parser import parse_all, parse_rows
from finagent.slots import extract, months, route_rules, won_amounts
from finagent.validate import check_answer, financial_values

ROOT = Path(__file__).resolve().parents[1]
HAS_RAW = any((ROOT / "data/raw").glob("*.json"))


# ---------- 금액·기간 ----------
@pytest.mark.parametrize("q,v", [("5천만원", [50_000_000]), ("1억 5천만원 대출", [150_000_000]),
                                 ("5조 8000억", [5_800_000_000_000]), ("월 50만원씩", [500_000]),
                                 ("500,000원", [500_000]), ("2억 빌려서", [200_000_000]),
                                 ("12개월 적금", []), ("750점이면", [])])
def test_won_amounts(q, v):
    assert won_amounts(q) == v


@pytest.mark.parametrize("q,v", [("2년 반", 30), ("30년", 360), ("6개월", 6), ("안녕", None)])
def test_months(q, v):
    assert months(q) == v


# ---------- 계산 ----------
def test_calculations_match_golden_and_reject_bad_input():
    assert saving_interest(500_000, 12, 2.45) == 79_625
    assert loan_payment(100_000_000, 360, 4.5) == 506_685
    assert loan_payment(100_000_000, 12, 6.0, "bullet") == 500_000
    assert deposit_interest(10_000_000, 12, 3.0) == 300_000
    assert deposit_interest(10_000_000, 12, 3.0, compound=True) > 300_000
    with pytest.raises(ValueError):
        saving_interest(0, 12, 3.0)
    with pytest.raises(ValueError):
        loan_payment(1, 12, 3.0, "unknown")


@pytest.mark.parametrize("score,code", [(901, "crdt_grad_1"), (900, "crdt_grad_4"), (750, "crdt_grad_5"),
                                        (501, "crdt_grad_10"), (300, "crdt_grad_13"), (0, "crdt_grad_13")])
def test_score_band_boundaries(score, code):
    assert score_band(score)[0] == code


def test_score_band_rejects_out_of_range():
    with pytest.raises(ValueError):
        score_band(1001)


# ---------- 상품 이름 매칭·조회 (합성) ----------
def _saving(code, bank_no, bank, name, rate):
    b = {"dcls_month": "202609", "fin_co_no": bank_no, "fin_prdt_cd": code, "kor_co_nm": bank, "fin_prdt_nm": name,
         "join_way": "", "dcls_end_day": "", "fin_co_subm_day": "", "mtrt_int": "", "spcl_cnd": "", "join_deny": "1",
         "join_member": "", "etc_note": "", "max_limit": None}
    o = {"dcls_month": "202609", "fin_co_no": bank_no, "fin_prdt_cd": code, "intr_rate_type_nm": "단리",
         "rsrv_type_nm": "자유적립식", "save_trm": "12", "intr_rate": rate, "intr_rate2": rate + 1}
    return parse_rows("saving", "bank", [b], [o])[0]


def test_generic_name_needs_bank_and_ties_are_kept():
    cat = Catalog([_saving("A", "1", "가은행", "정기적금", 3.0), _saving("B", "2", "나은행", "정기적금", 3.0),
                   _saving("C", "3", "다은행", "다은행 특별적금", 2.0)])
    assert cat.find_products("정기적금 금리 알려줘") == []  # 여러 회사가 쓰는 이름
    assert [p.bank for p in cat.find_products("나은행 정기적금 금리")] == ["나은행"]
    rows = cat.query("saving", limit=1)
    assert {r["bank"] for r in rows} == {"가은행", "나은행"}  # 1위 동률은 모두 포함


def test_exact_full_name_beats_parenthesis_variant():
    cat = Catalog([_saving("A", "1", "가은행", "가은행주택(아파트)", 3.0), _saving("B", "1", "가은행", "가은행주택", 3.0)])
    assert [p.name for p in cat.find_products("가은행주택(아파트) 금리")] == ["가은행주택(아파트)"]


def test_category_only_name_needs_bank_name():
    cat = Catalog([_saving("K", "9", "케이은행", "신용대출", 3.0), _saving("A", "1", "가은행", "e-정기적금", 3.0)])
    assert cat.find_products("은행 일반신용대출 중 제일 낮은 곳") == []
    assert cat.find_products("정기적금 금리") == []
    assert [p.bank for p in cat.find_products("케이은행 신용대출 금리")] == ["케이은행"]


def test_citation_product_id_brackets_are_normalized():
    assert check_answer("0.5%p", allowed([0.5]), [{"product_id": "[p1]", "quote": "급여 0.5%p"}], {"p1": "급여 0.5%p"})[0]


def test_credit_query_requires_score():
    with pytest.raises(ValueError, match="신용점수"):
        Catalog([]).query("credit")


# ---------- 검증 ----------
def test_financial_values_units():
    assert financial_values("79,625원, 연 2.45%, 우대 0.7%p, 50만원, 1억") == {79625.0, 2.45, 0.7, 500000.0, 1e8}


def allowed(pct=(), won=()):
    return {"pct": set(pct), "won": set(won)}


def test_check_answer_rejects_unsupported_number_and_fake_quote():
    al = allowed(pct=[2.45, 3.85], won=[79625])
    sec = {"p1": "가.급여/연금 이체:연 0.7%p"}
    assert check_answer("이자는 79,625원, 금리 2.45%예요 1) 2)", al, [], sec) == (True, None)
    ok, why = check_answer("이자는 147,000원이에요", al, [], sec)
    assert not ok and "147000원" in why
    assert not check_answer("0.7%p 더 줘요", allowed([0.7]), [{"product_id": "p1", "quote": "급여이체 시 1%p"}], sec)[0]
    assert check_answer("0.7%p 더 줘요", allowed([0.7]), [{"product_id": "p1", "quote": "급여/연금 이체:연 0.7%p"}], sec)[0]
    assert not check_answer("금리는 2.45%예요", al, [], sec, required=[(79625, 67363)])[0]
    assert check_answer("세후 67,363원이에요", allowed(won=[67363]), [], sec, required=[(79625, 67363)])[0]


def test_term_number_does_not_validate_a_rate():
    """기간 12(개월)가 근거에 있어도 금리 12%는 통과하면 안 된다(리뷰 재현 케이스)."""
    from finagent.schemas import AnswerInput, ProductDoc, RateRow
    from finagent.validate import allowed_values
    inp = AnswerInput(question="가나적금 12개월 기본금리 얼마야?",
                      rows=[RateRow(product_id="p", bank="가", product="가나", option="12개월", metric="기본금리", value=2.45)],
                      documents=[ProductDoc(product_id="p", title="t", info={}, rate_table="| 12 | 정액 | 2.45 | 3.85 |")])
    al = allowed_values(inp, inp.question)
    assert al["pct"] == {2.45, 3.85} and 12.0 not in al["pct"]
    assert not check_answer("12개월 상품의 기본금리는 12%예요", al, [], {})[0]
    assert check_answer("12개월 기본금리는 2.45%, 우대 포함 3.85%예요", al, [], {})[0]


def test_table_quote_rejected_with_actionable_reason():
    ok, why = check_answer("5.00%예요", allowed([5.0]), [{"product_id": "p", "quote": "최고금리 | 5.00"}], {"p": "최고금리 | 5.00"})
    assert not ok and "상품 설명" in why


def test_winner_must_be_first_mentioned():
    w = (["국민은행", "일반신용대출"], ["국민은행", "일반신용대출", "케이뱅크", "신용대출"])
    al = allowed([6.19, 7.12])
    assert not check_answer("케이뱅크가 7.12%로 가장 낮아요 (국민은행 6.19%)", al, [], {}, winner=w)[0]
    assert check_answer("국민은행 일반신용대출이 6.19%로 가장 낮아요", al, [], {}, winner=w)[0]


def test_metric_whitelist_blocks_average_rate_ranking():
    cat = Catalog([_saving("A", "1", "가은행", "가나적금", 3.0)])
    with pytest.raises(ValueError, match="평균금리"):
        cat.query("saving", metric="avg")
    with pytest.raises(ValueError, match="평균금리"):
        cat.query("mortgage", metric="avg")
    with pytest.raises(ValueError, match="신용점수 구간"):
        cat.query("credit", metric="min", credit_score=700)
    assert cat.query("saving", metric="rate_max")[0]["value"] == 4.0


# ---------- 실데이터 회귀 ----------
@pytest.mark.skipif(not HAS_RAW, reason="data/raw 없음")
def test_rule_routing_on_routing_set_and_golden():
    cat = Catalog(parse_all(ROOT / "data/raw"))
    for fn, label in (("eval/routing_set.jsonl", "route"), ("eval/golden.jsonl", None)):
        for line in (ROOT / fn).read_text().splitlines():
            r = json.loads(line)
            if r.get("setup"):  # 후속 질문은 직전 대화 슬롯이 있어야 판단 가능 — 그래프 테스트에서 검증
                continue
            gold = r["route"] if label else ({"chat": "chat", "out_of_scope": "out_of_scope"}.get(r["category"])
                                             or r["difficulty"])
            assert route_rules(r["query"], extract(r["query"], cat))[0] == gold, r["id"]


@pytest.mark.skipif(not HAS_RAW, reason="data/raw 없음")
def test_catalog_reproduces_golden_compare_answers():
    cat = Catalog(parse_all(ROOT / "data/raw"))
    golden = {json.loads(l)["id"]: json.loads(l) for l in (ROOT / "eval/golden.jsonl").read_text().splitlines()}
    for gid in ("H1", "H2", "H5", "H7"):
        s = extract(golden[gid]["query"], cat)
        rows = cat.query(s.kinds[0], group=s.group, term_months=s.term_months, metric=s.metric,
                         credit_score=s.credit_score, filters=s.filters, limit=1)
        assert set(golden[gid]["check"]["values"]) & ({r["product"] for r in rows} | {r["bank"] for r in rows}), gid


def test_split_citations_keeps_only_verbatim_quotes():
    from finagent.validate import split_citations
    kept, dropped = split_citations([{"product_id": "[p]", "quote": "급여 0.5%p"}, {"product_id": "p", "quote": "해당없음"},
                                     {"product_id": "x", "quote": "급여 0.5%p"}], {"p": "급여 0.5%p 제공"})
    assert kept == [{"product_id": "p", "quote": "급여 0.5%p"}] and len(dropped) == 2


