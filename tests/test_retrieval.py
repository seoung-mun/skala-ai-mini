import numpy as np

from finagent.retrieval import HybridRetriever
from finagent.search import KeywordIndex

MD = """---
id: "{id}"
---

# {title}

<!-- retrieval: keyword -->
## 기본 정보

{kw}

<!-- retrieval: embedding -->
## 상품 설명

### 우대조건

{emb}
"""


class FakeEmbedder:
    """'급여'·'월급'을 같은 축으로 보는 장난감 임베딩 — 의미 검색이 키워드를 보완하는지 확인용."""

    name = "fake"
    axes = [("급여", "월급"), ("자동이체",), ("대출",)]

    def encode(self, texts):
        """정해진 단어 축에 대한 포함 여부로 정규화 벡터를 만든다."""
        v = np.array(
            [
                [float(any(w in t for w in ax)) for ax in self.axes] + [0.01]
                for t in texts
            ],
            dtype="float32",
        )
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def make(tmp_path=None):
    """테스트 문서 3개로 HybridRetriever를 만든다."""
    docs = {
        "a": MD.format(
            id="a", title="가은행 A적금", kw="적금", emb="급여 이체 시 0.5%p"
        ),
        "b": MD.format(
            id="b", title="나은행 B적금", kw="적금", emb="자동이체 시 0.2%p"
        ),
        "c": "# 다은행 신용대출\n<!-- retrieval: keyword -->\n## 금리\n표",
    }
    return HybridRetriever(
        KeywordIndex(docs), FakeEmbedder(), cache_dir=tmp_path
    )


def test_dense_side_finds_paraphrase_and_only_embedding_sections_are_indexed():
    """임베딩은 embedding 섹션만 색인하고 바꿔 말한 질문도 찾는다."""
    r = make()
    assert r.dense_ids == [
        "a",
        "b",
    ]  # 텍스트 섹션 없는 신용대출은 키워드로만 찾는다
    assert r.search("월급 들어오면 우대 얼마", k=1)[0][0] == "a"


def test_allowed_filter_restricts_results():
    """allowed가 결과를 그 id 안으로 제한한다."""
    r = make()
    assert [i for i, _ in r.search("적금 우대", k=3, allowed={"b"})] == ["b"]


def test_reranker_reorders_only_the_top_pool():
    """리랭커는 상위 후보 풀만 다시 정렬한다."""

    class PreferB:
        """'자동이체' 문서를 1위로 올리는 가짜 리랭커."""

        calls = []

        def scores(self, q, texts):
            """호출 크기를 기록하고 '자동이체' 포함 문서에 높은 점수를 준다."""
            PreferB.calls.append(len(texts))
            return [1.0 if "자동이체" in t else 0.0 for t in texts]

    docs = make().index.docs
    r = HybridRetriever(
        KeywordIndex(docs), FakeEmbedder(), reranker=PreferB(), rerank_pool=2
    )
    assert r.search("월급 들어오면 우대", k=1)[0][
        0
    ] == "b" and PreferB.calls == [2]


def test_reranker_skipped_when_candidates_fit_in_k():
    """후보가 k개 이하면 리랭커를 부르지 않는다."""

    class Count:
        """호출 횟수만 세는 가짜 리랭커."""

        calls = 0

        def scores(self, q, texts):
            """호출 횟수를 세고 0점을 돌려준다."""
            Count.calls += 1
            return [0.0] * len(texts)

    r = HybridRetriever(
        KeywordIndex(make().index.docs), FakeEmbedder(), reranker=Count()
    )
    assert {i for i, _ in r.search("적금 우대", k=3, allowed={"a", "b"})} == {
        "a",
        "b",
    } and Count.calls == 0
    r.search("적금 우대", k=1, allowed={"a", "b"})
    assert Count.calls == 1


def test_embedding_cache_reused(tmp_path):
    """같은 입력이면 임베딩 캐시 파일을 다시 쓴다."""
    make(tmp_path)
    files = list(tmp_path.glob("emb_*.npy"))
    assert len(files) == 1
    make(tmp_path)
    assert list(tmp_path.glob("emb_*.npy")) == files
