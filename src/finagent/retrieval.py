"""하이브리드 검색: BM25(전체 마크다운) + 임베딩(embedding 섹션만) -> RRF 결합.

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


class HybridRetriever:
    def __init__(self, index: KeywordIndex, embedder, cache_dir: Path | None = None):
        self.index = index
        self.embedder = embedder
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
        return sorted(score.items(), key=lambda x: (-x[1], x[0]))[:k]


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
