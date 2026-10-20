"""그래프 노드가 쓰는 의존성을 한곳에 모아 functools.partial로 주입한다(베이스 Wave 13과 같은 방식).
테스트에서는 가짜 LLM·가짜 MCP로 갈아 끼운다."""
from dataclasses import dataclass
from typing import Any

from .catalog import Catalog


@dataclass
class Deps:
    llm: Any          # chat(system, user, schema=None) -> (text, usage)
    mcp: Any          # call_tool(name, args) -> (dict, retries), get_prompt(name, args) -> str
    retriever: Any    # search(query, k, allowed) -> [(id, score)]
    catalog: Catalog  # 슬롯 추출·상품 메타 (순수 조회)
    data_month: str
