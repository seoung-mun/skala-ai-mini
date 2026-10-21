"""키워드 검색(BM25). 형태소 분석기 없이 한글 단어를 2글자 단위로 쪼개 조사가 붙어도 매칭되게 한다."""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path


def tokenize(text: str) -> list[str]:
    """한글은 2글자 단위, 영문·숫자는 단어 단위로 토큰화한다(한 글자 한글은 버린다)."""
    out: list[str] = []
    for w in re.findall(r"[가-힣]+|[a-z]+|\d+(?:\.\d+)?", text.lower()):
        if w[0] >= "가":
            out += [
                w[i : i + 2] for i in range(len(w) - 1)
            ]  # 한 글자 한글(연·월·의)은 버린다
        else:
            out.append(w)
    return out


class KeywordIndex:
    """문서 dict 위의 BM25 키워드 색인."""

    def __init__(self, docs: dict[str, str], k1: float = 1.5, b: float = 0.75):
        """문서별 단어 빈도·길이·IDF를 계산한다. 문서가 없으면 ValueError."""
        if not docs:
            raise ValueError("색인할 문서가 없습니다")
        self.docs = docs
        self.k1, self.b = k1, b
        self.tf = {i: Counter(tokenize(t)) for i, t in docs.items()}
        self.len = {i: sum(c.values()) for i, c in self.tf.items()}
        self.avg = sum(self.len.values()) / len(self.len)
        df = Counter(tok for c in self.tf.values() for tok in c)
        n = len(docs)
        self.idf = {
            tok: math.log(1 + (n - d + 0.5) / (d + 0.5))
            for tok, d in df.items()
        }

    @classmethod
    def from_dir(cls, md_dir: Path) -> "KeywordIndex":
        """디렉터리의 .md 파일(파일명=문서 id)로 색인을 만든다. id가 겹치면 ValueError."""
        docs: dict[str, str] = {}
        for f in sorted(md_dir.rglob("*.md")):
            if f.stem in docs:
                raise ValueError(f"문서 id 중복: {f.stem}")
            docs[f.stem] = f.read_text(encoding="utf-8")
        return cls(docs)

    def search(self, query: str, k: int = 5) -> list[tuple[str, float]]:
        """BM25 점수가 0보다 큰 문서를 (id, 점수) 순으로 k개까지 돌려준다."""
        q = Counter(tokenize(query))
        scores = {}
        for i, tf in self.tf.items():
            norm = self.k1 * (1 - self.b + self.b * self.len[i] / self.avg)
            s = sum(
                self.idf.get(t, 0) * tf[t] * (self.k1 + 1) / (tf[t] + norm)
                for t in q
                if t in tf
            )
            if s > 0:
                scores[i] = s
        return sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:k]
