"""하이브리드 검색: BM25(전체 마크다운) + 임베딩(embedding 섹션만) -> RRF 결합 -> (선택) 크로스인코더 리랭킹.

임베딩은 우대조건·수수료 같은 자유 텍스트에만 쓴다(금리표·이름은 키워드/구조화 조회가 더 정확).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from .parser import split_sections
from .search import KeywordIndex

RRF_K = 60


class KureEmbedder:
    """fp16 로딩: MPS 할당 3.0GB -> 1.1GB. 문서 벡터 코사인 fp32 대비 최소 0.9997, 상위 50위 동일(실측, ADR-0011).

    dtype을 캐시 키(name)에 넣어 다른 정밀도로 만든 벡터를 섞지 않는다.
    """

    def __init__(
        self, model: str = "nlpai-lab/KURE-v1", dtype: str = "float16"
    ):
        """SentenceTransformer 모델을 지정한 정밀도로 불러온다."""
        from sentence_transformers import (
            SentenceTransformer,
        )  # 무거워서 실제 사용 시에만 import

        self.name = f"{model}@{dtype}"
        self.model = SentenceTransformer(
            model, model_kwargs={"torch_dtype": dtype}
        )

    def encode(self, texts: list[str]) -> np.ndarray:
        """텍스트 목록을 정규화된 float32 벡터로 만든다."""
        return np.asarray(
            self.model.encode(texts, normalize_embeddings=True, batch_size=32),
            dtype="float32",
        )


class CrossEncoderReranker:
    """상위 후보만 다시 정렬한다. 실측: 쉬운 질문 top-1 18/20 -> 20/20, 질의당 ~330ms, 메모리 ~0.4GB(fp16)."""

    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3"):
        """크로스인코더 모델을 fp16으로 불러온다."""
        from sentence_transformers import (
            CrossEncoder,
        )  # 무거워서 실제 사용 시에만 import

        self.model = CrossEncoder(
            model, model_kwargs={"torch_dtype": "float16"}
        )

    def scores(self, query: str, texts: list[str]) -> list[float]:
        """(질의, 문서) 쌍마다 관련도 점수를 돌려준다."""
        return [
            float(x) for x in self.model.predict([(query, t) for t in texts])
        ]


class HybridRetriever:
    """BM25와 임베딩 순위를 RRF로 합치고, 리랭커가 있으면 상위 후보만 다시 정렬하는 검색기."""

    def __init__(
        self,
        index: KeywordIndex,
        embedder,
        cache_dir: Path | None = None,
        reranker=None,
        rerank_pool: int = 5,
    ):
        """임베딩 대상 문서를 골라 벡터를 만든다(cache_dir이 있으면 캐시를 재사용)."""
        self.index = index
        self.embedder = embedder
        self.reranker, self.rerank_pool = reranker, rerank_pool
        self.dense_ids = [i for i, md in index.docs.items() if _embed_text(md)]
        texts = [_embed_text(index.docs[i]) for i in self.dense_ids]
        self.vectors = _cached_encode(embedder, texts, cache_dir)

    def search(
        self, query: str, k: int = 3, allowed: set[str] | None = None
    ) -> list[tuple[str, float]]:
        """allowed 범위 안에서 하이브리드 검색해 (상품 id, 점수)를 k개까지 돌려준다."""
        ok = (lambda i: True) if allowed is None else (lambda i: i in allowed)
        bm25 = [
            i
            for i, _ in self.index.search(query, len(self.index.docs))
            if ok(i)
        ]
        q = self.embedder.encode([query])[0]
        order = np.argsort(-(self.vectors @ q)) if len(self.dense_ids) else []
        dense = [self.dense_ids[j] for j in order if ok(self.dense_ids[j])]
        score: dict[str, float] = {}
        for ranks in (bm25, dense):
            for r, i in enumerate(ranks[:50]):
                score[i] = score.get(i, 0.0) + 1 / (RRF_K + r + 1)
        fused = sorted(score.items(), key=lambda x: (-x[1], x[0]))
        if (
            self.reranker is None or len(fused) <= k
        ):  # 후보가 k개 이하면 리랭킹해도 반환 집합이 같다(순서만 다름)
            return fused[:k]
        pool = [i for i, _ in fused[: max(k, self.rerank_pool)]]
        s = self.reranker.scores(query, [self.rerank_text(i) for i in pool])
        return sorted(zip(pool, s), key=lambda x: (-x[1], x[0]))[:k]

    def rerank_text(self, doc_id: str) -> str:
        """리랭커에 넣을 문서 텍스트(임베딩 섹션, 없으면 제목)."""
        md = self.index.docs[doc_id]
        return _embed_text(md) or next(
            (ln for ln in md.splitlines() if ln.startswith("# ")), md[:200]
        )


def _embed_text(md: str) -> str:
    """마크다운에서 제목과 embedding 섹션만 뽑는다. 해당 섹션이 없으면 빈 문자열."""
    title = next((ln for ln in md.splitlines() if ln.startswith("# ")), "")
    body = "\n".join(t for kind, t in split_sections(md) if kind == "embedding")
    return f"{title}\n{body}" if body else ""


def _cached_encode(
    embedder, texts: list[str], cache_dir: Path | None
) -> np.ndarray:
    """텍스트 벡터를 만든다. 모델명·텍스트 해시가 같은 캐시 파일이 있으면 그것을 읽는다."""
    if not texts:
        return np.zeros((0, 1), dtype="float32")
    if cache_dir is None:
        return embedder.encode(texts)
    key = hashlib.sha256(
        ("\x00".join([embedder.name, *texts])).encode()
    ).hexdigest()[:16]
    path = cache_dir / f"emb_{key}.npy"
    if path.exists():
        return np.load(path)
    vec = embedder.encode(texts)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.save(path, vec)
    return vec
