from finagent.baseline import run_baseline
from finagent.scoring import numbers, score
from finagent.search import KeywordIndex, tokenize


def test_tokenize_splits_korean_into_bigrams_so_particles_still_match():
    """한글을 2글자 단위로 나눠 조사가 붙어도 매칭된다."""
    assert set(tokenize("적금")) <= set(tokenize("적금은 어때"))
    assert tokenize("연 3.85%") == [
        "3.85"
    ]  # 한 글자 한글은 버리고 숫자는 그대로


def test_keyword_index_ranks_matching_doc_first_and_ignores_unrelated():
    """BM25가 맞는 문서를 1위로 두고 무관한 질의는 빈 결과를 낸다."""
    idx = KeywordIndex(
        {
            "a": "카카오뱅크 26주적금 자동이체",
            "b": "국민은행 주택담보대출 고정금리",
        }
    )
    assert idx.search("26주적금 우대금리")[0][0] == "a"
    assert idx.search("전혀무관한질의") == []


def test_from_dir_rejects_duplicate_doc_ids(tmp_path):
    """from_dir은 문서 id 중복을 거부한다."""
    import pytest

    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "same.md").write_text("x")
    with pytest.raises(ValueError, match="중복"):
        KeywordIndex.from_dir(tmp_path)


def test_numbers_strip_commas():
    """numbers가 쉼표를 지우고 숫자를 뽑는다."""
    assert numbers("세전이자 79,625원, 연 2.45%") == [79625.0, 2.45]


def item(check, gold=("x",), retrieval="any"):
    """채점용 골든 문항 dict를 만든다."""
    return {
        "check": check,
        "gold_ids": list(gold),
        "retrieval": retrieval if gold else None,
    }


def test_score_numeric_and_retrieval():
    """수치 채점과 검색 적중 판정을 확인한다."""
    r = {"answer": "최고 연 3.00%p입니다", "retrieved": ["x", "y"]}
    assert score(item({"type": "nums_any", "values": [3.0]}), r) == {
        "correct": True,
        "retrieval_hit": True,
    }
    assert score(
        item(
            {"type": "nums_all", "values": [3.0, 2.45]},
            gold=("x", "z"),
            retrieval="all",
        ),
        r,
    ) == {"correct": False, "retrieval_hit": False}


def test_score_first_asserted_value_must_be_gold_and_not_negated():
    """답이 처음 주장한 값이 정답이어야 하고 부정되면 오답이다."""
    q = {
        "check": {"type": "nums_any", "values": [2.45]},
        "gold_ids": [],
        "retrieval": None,
        "query": "12개월 기본금리?",
    }

    def a(s):
        return score(q, {"answer": s, "retrieved": []})["correct"]

    assert a("기본금리는 2.45%이고 우대 포함 3.85%예요")
    assert not a("2.45%가 아니라 99.9%입니다")  # 리뷰 재현: 부정 후 다른 값
    assert not a("99.9%입니다. 참고로 작년엔 2.45%")  # 처음 주장한 값이 틀림
    assert not a("2.45입니다")  # 단위 없는 숫자는 주장으로 보지 않음


def test_score_ignores_echoed_inputs_from_question():
    """질문의 입력값을 되풀이한 숫자는 채점에서 뺀다."""
    q = {
        "check": {"type": "nums_any", "values": [79625]},
        "gold_ids": [],
        "retrieval": None,
        "query": "월 50만원씩 12개월 넣으면 이자 얼마야?",
    }
    assert score(
        q,
        {
            "answer": "월 50만원씩 넣으면 세전이자는 79,625원이에요",
            "retrieved": [],
        },
    )["correct"]


def test_score_names_ignore_spaces_and_refusal_rejects_made_up_price():
    """이름 채점은 공백을 무시하고, 거절 채점은 지어낸 금액을 오답으로 본다."""
    assert score(
        item({"type": "names_any", "values": ["KDB 자유적금"]}),
        {"answer": "KDB자유적금이에요", "retrieved": []},
    )["correct"]
    ok = {"answer": "주가 정보는 문서에 없어요", "retrieved": []}
    bad = {"answer": "확인할 수 없지만 대략 72,000원입니다", "retrieved": []}
    assert score(item({"type": "refusal"}, gold=()), ok)["correct"]
    assert not score(item({"type": "refusal"}, gold=()), bad)["correct"]


def test_top_choice_uses_first_mentioned_candidate_not_any_mention():
    """1위 채점은 처음 언급한 후보로 판정한다."""
    chk = {
        "type": "top_choice",
        "values": ["국민은행"],
        "candidates": ["국민은행", "신한은행", "광주은행"],
    }
    wrong = {
        "answer": "가장 낮은 곳은 신한은행입니다. (국민은행 6.19%보다 낮음)",
        "retrieved": [],
    }
    right = {
        "answer": "국민은행이 6.19%로 가장 낮고 신한은행은 7.36%예요",
        "retrieved": [],
    }
    assert not score(item(chk, gold=()), wrong)["correct"]
    assert score(item(chk, gold=()), right)["correct"]


def test_top_choice_named_products_in_question_still_judged_by_first_pick():
    """질문에 후보 이름이 있어도 답이 처음 고른 것으로 판정한다."""
    chk = {
        "type": "top_choice",
        "values": ["카카오뱅크 26주적금"],
        "candidates": ["카카오뱅크 26주적금", "토스뱅크 굴비 적금"],
    }
    it = {
        **item(chk, gold=()),
        "query": "카카오뱅크 26주적금이랑 토스뱅크 굴비 적금 중에 뭐가 더 높아?",
    }
    assert score(
        it,
        {
            "answer": "카카오뱅크 26주적금(5.0%)이 굴비 적금(4.3%)보다 높아요",
            "retrieved": [],
        },
    )["correct"]
    assert not score(
        it,
        {
            "answer": "토스뱅크 굴비 적금이 카카오뱅크 26주적금보다 높아요",
            "retrieved": [],
        },
    )["correct"]


def test_golden_queries_do_not_require_product_names():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "user_golden", root / "eval/build_golden.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = module.make_suite()
    assert len(suite) == 43
    assert sum(i["category"] == "earnings" for i in suite) == 8
    assert sum(bool(i.get("setup")) for i in suite) >= 9
    for item in suite:
        if "query_args" in item.get("oracle", {}):
            rows = module.CAT.query(**item["oracle"]["query_args"])
            assert set(item["gold_ids"]) == {r["product_id"] for r in rows}


def test_first_mention_prefers_longer_name_at_same_position():
    """같은 위치면 더 긴 이름을 처음 언급한 후보로 본다."""
    from finagent.scoring import first_mention

    assert (
        first_mention(
            "카카오뱅크 26주적금이 더 높아요",
            ["카카오뱅크", "카카오뱅크 26주적금"],
        )
        == "카카오뱅크26주적금"
    )


def test_user_recommendation_requires_winners_and_actual_earnings():
    check = {
        "type": "recommendation",
        "values": ["가은행 가적금", "나은행 나적금"],
        "candidates": ["가은행 가적금", "나은행 나적금", "다은행 다적금"],
        "value": 67362,
        "unit": "won",
    }
    it = item(check, gold=())
    good = "가은행 가적금과 나은행 나적금이 공동 1위이며 세후 이자는 67,362원입니다."
    assert score(it, {"answer": good, "retrieved": []})["correct"]
    for bad in (
        good.replace("67,362", "79,625"),
        good.replace("과 나은행 나적금", ""),
        "다은행 다적금이 1위입니다. " + good,
    ):
        assert not score(it, {"answer": bad, "retrieved": []})["correct"]


def test_clarification_requires_missing_fields_and_rejects_guaranteed_eligibility():
    it = item(
        {
            "type": "clarification",
            "groups": [["금액"], ["기간"]],
            "forbid_guarantee": True,
        },
        gold=(),
    )
    assert score(it, {"answer": "금액과 기간을 알려주세요.", "retrieved": []})[
        "correct"
    ]
    assert not score(
        it,
        {
            "answer": "금액과 기간은 상관없이 누구나 가입가능합니다.",
            "retrieved": [],
        },
    )["correct"]


class FakeLLM:
    """받은 프롬프트를 기록하고 고정 답을 내는 가짜 LLM."""

    def __init__(self):
        """호출 기록을 비운다."""
        self.calls = []

    def chat(self, system, user):
        """프롬프트를 기록하고 고정 답과 사용량을 돌려준다."""
        self.calls.append(user)
        return "3.85%입니다", {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "tokens": 12,
            "usd": 0.0,
        }


def test_baseline_passes_retrieved_docs_to_injected_llm():
    """베이스라인이 검색한 문서를 주입된 LLM에 넘긴다."""
    idx = KeywordIndex(
        {
            "woori": "우리은행 우리SUPER주거래적금 최고우대 3.85",
            "kb": "국민은행 대출",
        }
    )
    llm = FakeLLM()
    r = run_baseline("우리SUPER주거래적금 최고우대금리", idx, llm, k=1)
    assert (
        r["retrieved"] == ["woori"]
        and "3.85" in llm.calls[0]
        and r["llm_calls"] == 1
    )
