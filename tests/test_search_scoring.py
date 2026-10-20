from finagent.baseline import run_baseline
from finagent.scoring import numbers, score
from finagent.search import KeywordIndex, tokenize


def test_tokenize_splits_korean_into_bigrams_so_particles_still_match():
    assert set(tokenize("적금")) <= set(tokenize("적금은 어때"))
    assert tokenize("연 3.85%") == ["3.85"]  # 한 글자 한글은 버리고 숫자는 그대로


def test_keyword_index_ranks_matching_doc_first_and_ignores_unrelated():
    idx = KeywordIndex({"a": "카카오뱅크 26주적금 자동이체", "b": "국민은행 주택담보대출 고정금리"})
    assert idx.search("26주적금 우대금리")[0][0] == "a"
    assert idx.search("전혀무관한질의") == []


def test_from_dir_rejects_duplicate_doc_ids(tmp_path):
    import pytest
    for d in ("a", "b"):
        (tmp_path / d).mkdir(); (tmp_path / d / "same.md").write_text("x")
    with pytest.raises(ValueError, match="중복"):
        KeywordIndex.from_dir(tmp_path)


def test_numbers_strip_commas():
    assert numbers("세전이자 79,625원, 연 2.45%") == [79625.0, 2.45]


def item(check, gold=("x",), retrieval="any"):
    return {"check": check, "gold_ids": list(gold), "retrieval": retrieval if gold else None}


def test_score_numeric_and_retrieval():
    r = {"answer": "최고 연 3.00%p입니다", "retrieved": ["x", "y"]}
    assert score(item({"type": "nums_any", "values": [3.0]}), r) == {"correct": True, "retrieval_hit": True}
    assert score(item({"type": "nums_all", "values": [3.0, 2.45]}, gold=("x", "z"), retrieval="all"), r) == \
        {"correct": False, "retrieval_hit": False}


def test_score_first_asserted_value_must_be_gold_and_not_negated():
    q = {"check": {"type": "nums_any", "values": [2.45]}, "gold_ids": [], "retrieval": None, "query": "12개월 기본금리?"}
    a = lambda s: score(q, {"answer": s, "retrieved": []})["correct"]
    assert a("기본금리는 2.45%이고 우대 포함 3.85%예요")
    assert not a("2.45%가 아니라 99.9%입니다")          # 리뷰 재현: 부정 후 다른 값
    assert not a("99.9%입니다. 참고로 작년엔 2.45%")      # 처음 주장한 값이 틀림
    assert not a("2.45입니다")                         # 단위 없는 숫자는 주장으로 보지 않음


def test_score_ignores_echoed_inputs_from_question():
    q = {"check": {"type": "nums_any", "values": [79625]}, "gold_ids": [], "retrieval": None,
         "query": "월 50만원씩 12개월 넣으면 이자 얼마야?"}
    assert score(q, {"answer": "월 50만원씩 넣으면 세전이자는 79,625원이에요", "retrieved": []})["correct"]


def test_score_names_ignore_spaces_and_refusal_rejects_made_up_price():
    assert score(item({"type": "names_any", "values": ["KDB 자유적금"]}), {"answer": "KDB자유적금이에요", "retrieved": []})["correct"]
    ok = {"answer": "주가 정보는 문서에 없어요", "retrieved": []}
    bad = {"answer": "확인할 수 없지만 대략 72,000원입니다", "retrieved": []}
    assert score(item({"type": "refusal"}, gold=()), ok)["correct"]
    assert not score(item({"type": "refusal"}, gold=()), bad)["correct"]


def test_top_choice_uses_first_mentioned_candidate_not_any_mention():
    chk = {"type": "top_choice", "values": ["국민은행"], "candidates": ["국민은행", "신한은행", "광주은행"]}
    wrong = {"answer": "가장 낮은 곳은 신한은행입니다. (국민은행 6.19%보다 낮음)", "retrieved": []}
    right = {"answer": "국민은행이 6.19%로 가장 낮고 신한은행은 7.36%예요", "retrieved": []}
    assert not score(item(chk, gold=()), wrong)["correct"]
    assert score(item(chk, gold=()), right)["correct"]


def test_top_choice_named_products_in_question_still_judged_by_first_pick():
    chk = {"type": "top_choice", "values": ["카카오뱅크 26주적금"], "candidates": ["카카오뱅크 26주적금", "토스뱅크 굴비 적금"]}
    it = {**item(chk, gold=()), "query": "카카오뱅크 26주적금이랑 토스뱅크 굴비 적금 중에 뭐가 더 높아?"}
    assert score(it, {"answer": "카카오뱅크 26주적금(5.0%)이 굴비 적금(4.3%)보다 높아요", "retrieved": []})["correct"]
    assert not score(it, {"answer": "토스뱅크 굴비 적금이 카카오뱅크 26주적금보다 높아요", "retrieved": []})["correct"]


def test_golden_candidates_exclude_generic_names():
    import json
    from pathlib import Path
    g = {json.loads(l)["id"]: json.loads(l) for l in (Path(__file__).resolve().parents[1] / "eval/golden.jsonl").read_text().splitlines()}
    assert "정기예금" not in g["H2"]["check"]["candidates"] and "The든든예금(시즌2)" in g["H2"]["check"]["candidates"]


def test_first_mention_prefers_longer_name_at_same_position():
    from finagent.scoring import first_mention
    assert first_mention("카카오뱅크 26주적금이 더 높아요", ["카카오뱅크", "카카오뱅크 26주적금"]) == "카카오뱅크26주적금"


class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, system, user):
        self.calls.append(user)
        return "3.85%입니다", {"prompt_tokens": 10, "completion_tokens": 2, "tokens": 12, "usd": 0.0}


def test_baseline_passes_retrieved_docs_to_injected_llm():
    idx = KeywordIndex({"woori": "우리은행 우리SUPER주거래적금 최고우대 3.85", "kb": "국민은행 대출"})
    llm = FakeLLM()
    r = run_baseline("우리SUPER주거래적금 최고우대금리", idx, llm, k=1)
    assert r["retrieved"] == ["woori"] and "3.85" in llm.calls[0] and r["llm_calls"] == 1
