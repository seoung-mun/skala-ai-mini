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
    def __init__(self, model: str = "nlpai-lab/KURE-v1"):
        from sentence_transformers import SentenceTransformer  # 무거워서 실제 사용 시에만 import
        self.name = model
        self.model = SentenceTransformer(model)

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.model.encode(texts, normalize_embeddings=True, batch_size=32), dtype="float32")


class CrossEncoderReranker:
    """상위 후보만 다시 정렬한다. 실측: 쉬운 질문 top-1 18/20 -> 20/20, 질의당 ~330ms, 메모리 ~0.4GB(fp16)."""

    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3"):
        from sentence_transformers import CrossEncoder  # 무거워서 실제 사용 시에만 import
        self.model = CrossEncoder(model, model_kwargs={"torch_dtype": "float16"})

    def scores(self, query: str, texts: list[str]) -> list[float]:
        return [float(x) for x in self.model.predict([(query, t) for t in texts])]


class HybridRetriever:
    def __init__(self, index: KeywordIndex, embedder, cache_dir: Path | None = None, reranker=None,
                 rerank_pool: int = 5):
        self.index = index
        self.embedder = embedder
        self.reranker, self.rerank_pool = reranker, rerank_pool
        self.dense_ids = [i for i, md in index.docs.items() if _embed_text(md)]
        texts = [_embed_text(index.docs[i]) for i in self.dense_ids]
        self.vectors = _cached_encode(embedder, texts, cache_dir)

    def search(self, query: str, k: int = 3, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        ok = (lambda i: True) if allowed is None else (lambda i: i in allowed)
        bm25 = [i for i, _ in self.index.search(query, len(self.index.docs)) if ok(i)]
        q = self.embedder.encode([query])[0]
        order = np.argsort(-(self.vectors @ q)) if len(self.dense_ids) else []
        dense = [self.dense_ids[j] for j in order if ok(self.dense_ids[j])]
        score: dict[str, float] = {}
        for ranks in (bm25, dense):
            for r, i in enumerate(ranks[:50]):
                score[i] = score.get(i, 0.0) + 1 / (RRF_K + r + 1)
        fused = sorted(score.items(), key=lambda x: (-x[1], x[0]))
        if self.reranker is None or len(fused) < 2:
            return fused[:k]
        pool = [i for i, _ in fused[:max(k, self.rerank_pool)]]
        s = self.reranker.scores(query, [self.rerank_text(i) for i in pool])
        return sorted(zip(pool, s), key=lambda x: (-x[1], x[0]))[:k]

    def rerank_text(self, doc_id: str) -> str:
        md = self.index.docs[doc_id]
        return _embed_text(md) or next((ln for ln in md.splitlines() if ln.startswith("# ")), md[:200])


def _embed_text(md: str) -> str:
    title = next((ln for ln in md.splitlines() if ln.startswith("# ")), "")
    body = "\n".join(t for kind, t in split_sections(md) if kind == "embedding")
    return f"{title}\n{body}" if body else ""


def _cached_encode(embedder, texts: list[str], cache_dir: Path | None) -> np.ndarray:
    if not texts:
        return np.zeros((0, 1), dtype="float32")
    if cache_dir is None:
        return embedder.encode(texts)
    key = hashlib.sha256(("\x00".join([embedder.name, *texts])).encode()).hexdigest()[:16]
    path = cache_dir / f"emb_{key}.npy"
    if path.exists():
        return np.load(path)
    vec = embedder.encode(texts)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.save(path, vec)
    return vec
