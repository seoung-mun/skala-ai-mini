"""MCP stdio 클라이언트. 서버 수명 동안 subprocess 1개를 재사용한다.

락을 두지 않는다: ClientSession은 요청 id로 응답을 짝지으므로 Send로 병렬 실행된 서브에이전트의 호출이
한 세션 위에서 동시에 진행된다(직렬화하면 병렬 서브에이전트가 도구 단계에서 다시 한 줄로 선다).

도구 결과는 항상 ToolResult로 돌려준다. 인자 오류·타임아웃도 ok=false로 담고, 연결 자체가 끊긴 경우만 예외다.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack

from langsmith import traceable
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .schemas import ToolResult

CALL_TIMEOUT_S = 30.0


async def with_retry(
    fn, *args, retries: int = 3, base_delay: float = 0.3, **kwargs
):
    """일시적 오류(TimeoutError)만 지수 백오프로 재시도. 반환: (결과, 재시도 횟수)."""
    for attempt in range(1, retries + 1):
        try:
            return await fn(*args, **kwargs), attempt - 1
        except asyncio.TimeoutError:
            if attempt == retries:
                raise
            await asyncio.sleep(base_delay * 3 ** (attempt - 1))


class MCPClient:
    """finagent MCP 서버를 subprocess(stdio)로 띄우고 한 세션으로 도구·Resource·Prompt를 호출한다."""

    def __init__(self):
        """연결 전 상태로 만든다. start()를 불러야 쓸 수 있다."""
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def start(self) -> None:
        """MCP 서버 subprocess를 띄우고 세션을 초기화한다."""
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "finagent.mcp_server"],
            env={**os.environ, "FASTMCP_LOG_LEVEL": "ERROR"},
        )
        read, write = await self._stack.enter_async_context(
            stdio_client(params)
        )
        self.session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        await self.session.initialize()

    async def stop(self) -> None:
        """세션과 subprocess를 닫는다. 이미 닫혔으면 아무것도 하지 않는다."""
        if self._stack is not None:
            await self._stack.aclose()
            self._stack, self.session = None, None

    async def _call(self, name: str, arguments: dict) -> ToolResult:
        """도구를 한 번 호출해 ToolResult로 바꾼다. 서버가 거부한 호출은 ok=False."""
        result = await asyncio.wait_for(
            self.session.call_tool(name, arguments), CALL_TIMEOUT_S
        )
        text = result.content[0].text if result.content else ""
        if result.isError:  # 인자 스키마 위반 등 서버가 거부한 호출
            return ToolResult(ok=False, tool_name=name, error=text)
        return ToolResult.model_validate(json.loads(text))

    @traceable(run_type="tool", name="mcp.call_tool")
    async def call_tool(
        self, name: str, arguments: dict
    ) -> tuple[ToolResult, int]:
        """도구 호출(타임아웃만 재시도). 재시도를 다 쓰면 예외 대신 ok=False 결과를 돌려준다."""
        try:
            return await with_retry(self._call, name, arguments)
        except asyncio.TimeoutError:
            return ToolResult(
                ok=False,
                tool_name=name,
                error=f"{CALL_TIMEOUT_S:.0f}초 안에 응답 없음(재시도 소진)",
            ), 2

    @traceable(run_type="tool", name="mcp.read_resource")
    async def read_resource(self, uri: str) -> str:
        """Resource URI의 첫 텍스트 본문을 돌려준다."""
        result = await self.session.read_resource(uri)
        return result.contents[0].text

    @traceable(run_type="tool", name="mcp.get_prompt")
    async def get_prompt(self, name: str, arguments: dict) -> str:
        """Prompt를 인자로 렌더링한 첫 메시지 텍스트를 돌려준다."""
        result = await self.session.get_prompt(name, arguments)
        return result.messages[0].content.text
