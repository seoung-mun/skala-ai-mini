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
        v = np.array([[float(any(w in t for w in ax)) for ax in self.axes] + [0.01] for t in texts], dtype="float32")
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def make(tmp_path=None):
    docs = {"a": MD.format(id="a", title="가은행 A적금", kw="적금", emb="급여 이체 시 0.5%p"),
            "b": MD.format(id="b", title="나은행 B적금", kw="적금", emb="자동이체 시 0.2%p"),
            "c": "# 다은행 신용대출\n<!-- retrieval: keyword -->\n## 금리\n표"}
    return HybridRetriever(KeywordIndex(docs), FakeEmbedder(), cache_dir=tmp_path)


def test_dense_side_finds_paraphrase_and_only_embedding_sections_are_indexed():
    r = make()
    assert r.dense_ids == ["a", "b"]  # 텍스트 섹션 없는 신용대출은 키워드로만 찾는다
    assert r.search("월급 들어오면 우대 얼마", k=1)[0][0] == "a"


def test_allowed_filter_restricts_results():
    r = make()
    assert [i for i, _ in r.search("적금 우대", k=3, allowed={"b"})] == ["b"]


def test_reranker_reorders_only_the_top_pool():
    class PreferB:
        calls = []

        def scores(self, q, texts):
            PreferB.calls.append(len(texts))
            return [1.0 if "자동이체" in t else 0.0 for t in texts]
    docs = make().index.docs
    r = HybridRetriever(KeywordIndex(docs), FakeEmbedder(), reranker=PreferB(), rerank_pool=2)
    assert r.search("월급 들어오면 우대", k=1)[0][0] == "b" and PreferB.calls == [2]


def test_embedding_cache_reused(tmp_path):
    make(tmp_path)
    files = list(tmp_path.glob("emb_*.npy"))
    assert len(files) == 1
    make(tmp_path)
    assert list(tmp_path.glob("emb_*.npy")) == files
