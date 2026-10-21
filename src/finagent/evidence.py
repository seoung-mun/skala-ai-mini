"""근거 검색 도메인 도구. 에이전트 프레임워크·모델을 모르고 deps의 Protocol(검색기·MCP·카탈로그)만 쓴다.

질문마다 하나 만든다. 본 후보(seen)와 고른 근거(picked)를 기록하고, 코드 게이트도 여기 둔다:
허용 범위 안에서만 검색하고, 실제로 본 후보만 읽고 고를 수 있다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .parser import KIND_KO, split_sections

READ_LIMIT = 1200  # 원문 한 번에 넘기는 글자 수


class EvidenceError(ValueError):
    """게이트에 걸린 요청. 호출한 쪽이 사유를 그대로 보여 준다."""


@dataclass
class EvidenceTools:
    """근거 검색 도구 묶음. 질문 하나 동안 본 후보·고른 근거·행동 기록을 들고 있다."""

    retriever: object
    mcp: object
    catalog: object
    allowed: set[str] | None = None
    seen: list[str] = field(default_factory=list)
    picked: list[str] | None = None
    trace: list[dict] = field(default_factory=list)

    def title(self, pid: str) -> str:
        """상품 id를 '상품id — 은행 상품명 (종류)' 한 줄로 만든다."""
        p = self.catalog.by_id[pid]
        return f"{pid} — {p.bank} {p.name} ({KIND_KO[p.kind]})"

    async def search(self, query: str, k: int = 5) -> list[str]:
        """허용 범위 안에서 하이브리드 검색. 찾은 상품 id를 점수 순으로 돌려주고 후보에 더한다."""
        hits = [
            i
            for i, _ in await asyncio.to_thread(
                self.retriever.search, query, k, self.allowed
            )
        ]
        self.seen = list(dict.fromkeys(self.seen + hits))
        self.trace.append({"action": "search", "arg": query})
        return hits

    async def read(self, pid: str) -> str:
        """후보 상품의 설명 원문(우대조건·가입 대상·유의사항)을 MCP Resource로 읽는다."""
        pid = pid.strip(" []")
        self.trace.append({"action": "read", "arg": pid})
        if pid not in self.seen:
            raise EvidenceError(f"후보에 없는 상품id: {pid}")
        md = await self.mcp.read_resource(f"product://{pid}")
        return (
            "\n".join(
                t for kind, t in split_sections(md) if kind == "embedding"
            )[:READ_LIMIT]
            or "(설명 없음)"
        )

    def select(self, ids: list[str], reason: str) -> list[str]:
        """근거를 확정한다. 본 후보가 아닌 id는 버린다."""
        self.picked = [
            i for i in (x.strip(" []") for x in ids) if i in self.seen
        ]
        self.trace.append(
            {"action": "finish", "arg": ", ".join(ids), "reason": reason}
        )
        return self.picked
