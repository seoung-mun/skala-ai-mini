from pathlib import Path

import pytest

from finagent.parser import (
    _num,
    _text,
    parse_all,
    parse_rows,
    split_sections,
    to_markdown,
)

RAW = Path(__file__).resolve().parents[1] / "data/raw"


def base(kind_fields=None, **kw):
    """테스트용 공통 기본 정보 행을 만든다."""
    b = {
        "dcls_month": "202609",
        "fin_co_no": "001",
        "fin_prdt_cd": "P1",
        "kor_co_nm": "가은행",
        "fin_prdt_nm": "상품",
        "join_way": "스마트폰",
        "dcls_end_day": "",
        "fin_co_subm_day": "x",
    }
    return {**b, **(kind_fields or {}), **kw}


def saving_base(**kw):
    """적금 기본 정보 행을 만든다."""
    return base(
        {
            "mtrt_int": "만기 후 연 0.1%",
            "spcl_cnd": "급여\t이체 시\n\n연 0.5%p ",
            "join_deny": "1",
            "join_member": "누구나",
            "etc_note": "",
            "max_limit": None,
        },
        **kw,
    )


def saving_opt(term="12", rate=3.0, rate_max=3.8, **kw):
    """적금 옵션 행을 만든다."""
    return {
        "dcls_month": "202609",
        "fin_co_no": "001",
        "fin_prdt_cd": "P1",
        "intr_rate_type_nm": "단리",
        "rsrv_type_nm": "정액적립식",
        "save_trm": term,
        "intr_rate": rate,
        "intr_rate2": rate_max,
        **kw,
    }


def loan_base(**kw):
    """대출 기본 정보 행을 만든다."""
    return base(
        {
            "loan_inci_expn": "인지세",
            "erly_rpay_fee": "1.4%",
            "dly_rate": "+7%",
            "loan_lmt": "LTV 70%",
        },
        **kw,
    )


def mort_opt(lo=2.97, hi=4.78, avg=3.2, **kw):
    """주담대 옵션 행을 만든다."""
    return {
        "dcls_month": "202609",
        "fin_co_no": "001",
        "fin_prdt_cd": "P1",
        "mrtg_type_nm": "아파트",
        "rpay_type_nm": "분할상환방식",
        "lend_rate_type_nm": "고정금리",
        "lend_rate_min": lo,
        "lend_rate_max": hi,
        "lend_rate_avg": avg,
        **kw,
    }


G = [
    "crdt_grad_1",
    "crdt_grad_4",
    "crdt_grad_5",
    "crdt_grad_6",
    "crdt_grad_10",
    "crdt_grad_11",
    "crdt_grad_12",
    "crdt_grad_13",
]


def credit_opt(code, name, vals, ptype="1", cd="C1"):
    """신용대출 옵션 행(금리 종류 하나, 점수 구간 값들)을 만든다."""
    return {
        "dcls_month": "202609",
        "fin_co_no": "001",
        "fin_prdt_cd": cd,
        "crdt_prdt_type": ptype,
        "crdt_lend_rate_type": code,
        "crdt_lend_rate_type_nm": name,
        "crdt_grad_avg": vals[0],
        **dict(zip(G, vals + [None] * (8 - len(vals)))),
    }


def credit_base(cd="C1", ptype="1", nm="일반신용대출"):
    """신용대출 기본 정보 행을 만든다."""
    return base(
        fin_prdt_cd=cd,
        crdt_prdt_type=ptype,
        crdt_prdt_type_nm=nm,
        cb_name="KCB",
    )


# ---------- 값 정리 ----------
@pytest.mark.parametrize(
    "raw,expected",
    [(".78", 0.78), ("-.09", -0.09), ("", None), (None, None), (3, 3.0)],
)
def test_num(raw, expected):
    """_num이 공시 숫자 표기를 float·None으로 바꾼다."""
    assert _num(raw) == expected


def test_num_rejects_garbage():
    """_num은 해석할 수 없는 값을 ValueError로 거부한다."""
    with pytest.raises(ValueError):
        _num("N/A")


def test_text_normalizes_whitespace_and_blank_lines():
    """_text가 공백과 빈 줄을 정리한다."""
    assert _text("a\t\t b \r\n\n  c  ") == "a b\nc"


# ---------- 조인·키 ----------
def test_option_joins_to_base_and_term_becomes_int():
    """옵션이 기본 정보와 합쳐지고 기간은 정수가 된다."""
    (p,) = parse_rows("saving", "bank", [saving_base()], [saving_opt("12")])
    assert p.options[0]["term_months"] == 12 and p.base["dcls_end_day"] is None


@pytest.mark.parametrize(
    "end,expected", [("", None), ("99991231", None), ("20261031", "20261031")]
)
def test_end_day_placeholder_is_not_an_end_date(end, expected):
    """종료일 자리표시(99991231)는 종료일로 보지 않는다."""
    (p,) = parse_rows(
        "saving", "bank", [saving_base(dcls_end_day=end)], [saving_opt()]
    )
    assert p.base["dcls_end_day"] == expected
    assert ("공시 종료일" in to_markdown(p)) == (expected is not None)


def test_orphan_option_fails_loudly():
    """기본 정보가 없는 옵션은 ValueError로 실패한다."""
    with pytest.raises(ValueError, match="상품 정보 없는 옵션"):
        parse_rows(
            "saving", "bank", [saving_base()], [saving_opt(fin_prdt_cd="ZZ")]
        )


def test_duplicate_product_key_fails_loudly():
    """상품 키 중복은 ValueError로 실패한다."""
    with pytest.raises(ValueError, match="키 중복"):
        parse_rows("saving", "bank", [saving_base(), saving_base()], [])


def test_credit_same_code_different_type_stay_separate_products():
    """코드가 같아도 대출 종류가 다른 신용대출은 별개 상품이다."""
    rows = [
        credit_base("WR", "2", "마이너스한도대출"),
        credit_base("WR", "3", "장기카드대출(카드론)"),
    ]
    opts = [
        credit_opt("A", "대출금리", [9.0], "2", "WR"),
        credit_opt("A", "대출금리", [12.0], "3", "WR"),
    ]
    a, b = parse_rows("credit", "bank", rows, opts)
    assert len(a.options) == 1 and len(b.options) == 1 and a.id != b.id


def test_product_without_options_is_kept_and_flagged():
    """옵션 없는 상품도 남기되 경고를 단다."""
    (p,) = parse_rows("mortgage", "bank", [loan_base()], [])
    assert p.flags == [
        "금리 옵션 없음"
    ] and "공시되지 않았습니다" in to_markdown(p)


# ---------- 데이터 주의 플래그 ----------
def test_mortgage_avg_outside_range_flagged():
    """평균금리가 최소~최대 범위 밖이면 경고를 단다."""
    (p,) = parse_rows(
        "mortgage", "bank", [loan_base()], [mort_opt(3.72, 5.28, 3.43)]
    )
    assert any("범위 밖" in f for f in p.flags)


def test_mortgage_missing_avg_flagged_and_ok_row_not():
    """평균금리 결측만 경고하고 정상 행은 경고하지 않는다."""
    (p,) = parse_rows("mortgage", "bank", [loan_base()], [mort_opt(avg=None)])
    assert any("평균금리 결측" in f for f in p.flags)
    (q,) = parse_rows("mortgage", "bank", [loan_base()], [mort_opt()])
    assert q.flags == []


def test_credit_identity_holds_regardless_of_option_order_and_negative_adjustment():
    """신용대출 금리 항등식(A=B+C-D)은 옵션 순서·음수 조정과 무관하게 맞는다."""
    # A = B + C - D (D가 음수인 칸 포함), 옵션 순서는 D,C,B,A
    opts = [
        credit_opt("D", "가감조정금리", [-0.09]),
        credit_opt("C", "가산금리", [3.17]),
        credit_opt("B", "기준금리", [0.83]),
        credit_opt("A", "대출금리", [4.09]),
    ]
    (p,) = parse_rows("credit", "bank", [credit_base()], opts)
    assert p.flags == []


def test_credit_identity_mismatch_flagged_and_empty_grades_skipped():
    """항등식 불일치는 경고하고 빈 점수 구간은 건너뛴다."""
    opts = [
        credit_opt("A", "대출금리", [9.0]),
        credit_opt("B", "기준금리", [1.0]),
        credit_opt("C", "가산금리", [3.0]),
        credit_opt("D", "가감조정금리", [0.0]),
    ]
    (p,) = parse_rows("credit", "bank", [credit_base()], opts)
    assert (
        len(p.flags) == 1 and "900점 초과" in p.flags[0]
    )  # 나머지 7구간은 None이라 검사 제외


# ---------- 마크다운 ----------
def test_saving_markdown_sections_and_pipes():
    """적금 마크다운의 섹션 표시와 표 안 '|' 처리를 확인한다."""
    (p,) = parse_rows(
        "saving", "bank", [saving_base(fin_prdt_nm="a|b")], [saving_opt()]
    )
    md = to_markdown(p)
    kinds = [k for k, _ in split_sections(md)]
    assert kinds == ["keyword", "keyword", "embedding"]
    emb = split_sections(md)[-1][1]
    assert "### 우대조건" in emb and "급여 이체 시\n연 0.5%p" in emb
    assert (
        "기타 유의사항" not in md
    )  # 비어 있는 텍스트 필드는 섹션을 만들지 않는다
    assert (
        "a\\|b" in md
        and "| 12 | 정액적립식 | 단리 | 3.00 | 3.80 |" in md
        and "| 금융권 | 은행 |" in md
    )
    assert "최고금리(우대 포함, %)" in md


def test_credit_has_no_embedding_section_and_renders_grades():
    """신용대출 문서는 임베딩 섹션 없이 점수 구간 금리표를 가진다."""
    (p,) = parse_rows(
        "credit",
        "bank",
        [credit_base()],
        [credit_opt("A", "대출금리", [2.62, 2.89])],
    )
    md = to_markdown(p)
    assert all(k == "keyword" for k, _ in split_sections(md))
    assert "| 900점 초과 | 2.62 |" in md and "| 300점 이하 | - |" in md


def test_multiline_product_name_becomes_single_line():
    """여러 줄 상품명은 한 줄로 합쳐진다."""
    (p,) = parse_rows(
        "deposit",
        "bank",
        [saving_base(fin_prdt_nm="제주Dream\n정기예금\n(개인)")],
        [saving_opt()],
    )
    assert (
        p.name == "제주Dream 정기예금 (개인)"
        and "# 가은행 제주Dream 정기예금 (개인) (정기예금)" in to_markdown(p)
    )


def test_same_code_in_different_kinds_gets_different_ids():
    """종류가 다르면 같은 상품코드라도 id가 다르다."""
    (m,) = parse_rows(
        "mortgage",
        "bank",
        [loan_base(fin_prdt_cd="WR1")],
        [mort_opt(fin_prdt_cd="WR1")],
    )
    (r,) = parse_rows("rent", "bank", [loan_base(fin_prdt_cd="WR1")], [])
    assert m.id != r.id


def test_filename_safe_id_for_slash_in_code():
    """상품코드의 '/'는 id에서 파일명에 안전한 문자로 바뀐다."""
    (p,) = parse_rows(
        "saving",
        "bank",
        [saving_base(fin_prdt_cd="SC1/SC2")],
        [saving_opt(fin_prdt_cd="SC1/SC2")],
    )
    assert "/" not in p.id


# ---------- 실데이터 (원본이 있을 때만) ----------
@pytest.mark.skipif(not any(RAW.glob("*.json")), reason="data/raw 없음")
def test_real_data_parses_and_credit_identity_holds_with_correct_key():
    """실데이터가 키 충돌 없이 파싱되고 신용대출 항등식이 성립한다."""
    products = parse_all(RAW)
    assert len({(p.kind, p.group, p.key) for p in products}) == len(products)
    credit_mismatch = [
        p
        for p in products
        if p.kind == "credit" and any("≠" in f for f in p.flags)
    ]
    assert credit_mismatch == []
