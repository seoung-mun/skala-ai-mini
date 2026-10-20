"""MCP stdio 클라이언트 (베이스 agent/mcp_client.py 축소판). 서버 수명 동안 subprocess 1개를 재사용한다."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack

from langsmith import traceable
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CALL_TIMEOUT_S = 30.0


class ToolFailed(RuntimeError):
    """도구가 오류를 돌려줌(잘못된 인자 등). 재시도하지 않는다."""


async def with_retry(fn, *args, retries: int = 3, base_delay: float = 0.3, **kwargs):
    """일시적 오류(TimeoutError)만 지수 백오프로 재시도. 반환: (결과, 재시도 횟수)."""
    for attempt in range(1, retries + 1):
        try:
            return await fn(*args, **kwargs), attempt - 1
        except asyncio.TimeoutError:
            if attempt == retries:
                raise
            await asyncio.sleep(base_delay * 3 ** (attempt - 1))


class MCPClient:
    def __init__(self):
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=sys.executable, args=["-m", "finagent.mcp_server"],
                                       env={**os.environ, "FASTMCP_LOG_LEVEL": "ERROR"})
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack, self.session = None, None

    async def _call(self, name: str, arguments: dict) -> dict:
        async with self._lock:
            result = await asyncio.wait_for(self.session.call_tool(name, arguments), CALL_TIMEOUT_S)
        text = result.content[0].text if result.content else ""
        if result.isError:
            raise ToolFailed(f"{name}: {text}")
        return json.loads(text)

    @traceable(run_type="tool", name="mcp.call_tool")
    async def call_tool(self, name: str, arguments: dict) -> tuple[dict, int]:
        return await with_retry(self._call, name, arguments)

    @traceable(run_type="tool", name="mcp.read_resource")
    async def read_resource(self, uri: str) -> str:
        async with self._lock:
            result = await self.session.read_resource(uri)
        return result.contents[0].text

    @traceable(run_type="tool", name="mcp.get_prompt")
    async def get_prompt(self, name: str, arguments: dict) -> str:
        async with self._lock:
            result = await self.session.get_prompt(name, arguments)
        return result.messages[0].content.text
