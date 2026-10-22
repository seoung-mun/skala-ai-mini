"""FastAPI 백엔드 (doc/API_CONTRACT.md v0.1.0).

실행: uv run python -m finagent.server  (127.0.0.1:8100, 로컬 Ollama 필요)
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .baseline import run_baseline
from .graph.build import build_graph, initial_state, next_slots, with_metadata
from .graph.nodes import WARNING
from .llm import session_context
from .schemas import LLMOutputError

ROOT = Path(__file__).resolve().parents[2]
MAX_MESSAGE = 500


@dataclass
class Runtime:
    """서버 실행 자원 묶음(버전·의존성·색인·세션 저장소·그래프)."""

    version: str
    deps: Any  # v2 Deps
    index: Any  # v1 키워드 색인
    sessions: Any
    llm_name: str
    graph: Any = None
    close: Any = None  # 종료 시 호출할 async 함수


async def default_runtime() -> Runtime:
    """실제 자원(카탈로그·LLM·MCP·하이브리드 검색기·세션 DB)으로 Runtime을 만든다."""
    from .catalog import Catalog
    from .config import load_config
    from .deps import Deps
    from .llm import build_agent_llm, build_llm
    from .mcp_client import MCPClient
    from .parser import parse_all
    from .retrieval import CrossEncoderReranker, HybridRetriever, KureEmbedder
    from .search import KeywordIndex
    from .session_store import SqliteSessionStore

    catalog = Catalog(parse_all(ROOT / "data/raw"))
    index = KeywordIndex.from_dir(ROOT / "data/md")
    cfg = load_config()
    llm = build_llm(cfg)
    mcp = MCPClient()
    await mcp.start()
    retriever = HybridRetriever(
        index,
        KureEmbedder(),
        ROOT / "data/emb_cache",
        reranker=CrossEncoderReranker() if cfg.rerank else None,
    )
    month = max(p.base["dcls_month"] for p in catalog.products)
    deps = Deps(
        llm=llm,
        mcp=mcp,
        retriever=retriever,
        catalog=catalog,
        data_month=month,
        planner=cfg.planner,
        rag_agent=cfg.rag_agent,
        agent_llm=build_agent_llm(cfg, llm),
    )
    return Runtime(
        cfg.supervisor_version,
        deps,
        index,
        SqliteSessionStore(ROOT / "session.db"),
        llm.name,
        close=mcp.stop,
    )


MAX_THREAD_ID = 100


class ChatRequest(BaseModel):
    """POST /chat 요청 본문."""

    message: str
    thread_id: str = "default"


class SessionResetRequest(BaseModel):
    thread_id: str


def thread_error(thread_id: str) -> str | None:
    if not thread_id.strip() or len(thread_id) > MAX_THREAD_ID:
        return f"thread_id는 1~{MAX_THREAD_ID}자여야 합니다"
    return None


def request_error(message: str, thread_id: str) -> str | None:
    """POST·스트림이 함께 쓰는 입력 검증. 문제가 있으면 사용자에게 보여줄 이유를 돌려준다."""
    if not message.strip():
        return "message가 비어 있습니다"
    if len(message) > MAX_MESSAGE:
        return f"message는 {MAX_MESSAGE}자 이하여야 합니다"
    return thread_error(thread_id)


def error_code(e: BaseException) -> str:
    """예외를 계약 오류 코드(LLM_UNAVAILABLE·TIMEOUT·INTERNAL)로 바꾼다."""
    if isinstance(e, httpx.HTTPError):
        return "LLM_UNAVAILABLE"
    if isinstance(e, LLMOutputError):
        return "INTERNAL"
    if isinstance(e, asyncio.TimeoutError):
        return "TIMEOUT"
    return "INTERNAL"


def create_app(make_runtime=default_runtime) -> FastAPI:
    """FastAPI 앱을 만든다. 테스트는 make_runtime으로 가짜 자원을 넣는다."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """시작할 때 Runtime·그래프를 만들고, 끝날 때 MCP·세션 DB를 닫는다."""
        rt = (
            await make_runtime()
        )  # 무거운 자원(MCP subprocess·임베딩 모델·그래프)은 프로세스당 1회
        rt.graph = build_graph(rt.deps)
        app.state.rt = rt
        yield
        if rt.close:
            await rt.close()
        rt.sessions.close()

    app = FastAPI(lifespan=lifespan)
    thread_locks: dict[str, asyncio.Lock] = {}

    def lock_for(thread_id):
        return thread_locks.setdefault(thread_id, asyncio.Lock())

    @app.post("/sessions/reset")
    async def reset_session(req: SessionResetRequest):
        if err := thread_error(req.thread_id):
            return JSONResponse({"code": "BAD_REQUEST", "message": err}, 400)
        try:
            async with lock_for(req.thread_id):
                app.state.rt.sessions.clear(req.thread_id)
        except Exception:
            return JSONResponse(
                {
                    "code": "INTERNAL",
                    "message": "서버 대화 이력을 초기화하지 못했습니다",
                },
                500,
            )
        return {"ok": True, "thread_id": req.thread_id}

    @app.exception_handler(RequestValidationError)
    async def bad_body(
        request, exc
    ):  # 본문 형식 오류도 계약의 오류 형식으로 돌려준다
        """요청 본문 형식 오류를 계약 오류 형식(400 BAD_REQUEST)으로 돌려준다."""
        return JSONResponse(
            {
                "code": "BAD_REQUEST",
                "message": f"요청 형식 오류: {exc.errors()[0]['msg']}",
            },
            400,
        )

    @app.get("/health")
    async def health():
        """상태 확인: 버전·LLM 이름·공시월을 돌려준다."""
        rt = app.state.rt
        return {
            "status": "ok",
            "version": rt.version,
            "llm": rt.llm_name,
            "data_month": rt.deps.data_month,
        }

    async def run_v1(rt: Runtime, message: str, thread_id: str) -> dict:
        """v1 베이스라인으로 답하고 계약 형식 FinalAnswer 본문을 만든다."""
        t0 = time.perf_counter()
        r = await asyncio.to_thread(
            run_baseline, message, rt.index, rt.deps.llm
        )
        m = rt.deps.data_month
        return {
            "reply": r["answer"],
            "route": "baseline",
            "products": [],
            "citations": [],
            "calculations": [],
            "warnings": [WARNING.format(y=m[:4], m=int(m[4:]))],
            "metadata": {
                "version": "v1",
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "llm_calls": 1,
                "retries": 0,
                "validation_attempts": 0,
                "cost_usd": r["usage"]["usd"],
                "data_month": m,
                "thread_id": thread_id,
                "prompt_tokens": r["usage"]["prompt_tokens"],
                "completion_tokens": r["usage"]["completion_tokens"],
            },
        }

    async def events(message: str, thread_id: str):
        # 초기화 완료 뒤 이전 요청이 과거 문맥을 다시 저장하지 않게 같은 대화만 직렬화한다.
        async with lock_for(thread_id):
            async for event in generate_events(message, thread_id):
                yield event

    async def generate_events(message: str, thread_id: str):
        """진행 이벤트(dict)들을 내보내고 마지막에 FinalAnswer를 내보낸다. 실패하면 Error."""
        rt = app.state.rt
        try:
            history, prev = rt.sessions.load(thread_id)
            with session_context(history):
                if rt.version == "v1":
                    final, slots = await run_v1(rt, message, thread_id), None
                else:
                    t0, state = time.perf_counter(), {}
                    async for mode, payload in rt.graph.astream(
                        initial_state(message, thread_id, history, prev),
                        stream_mode=["updates", "values", "custom"],
                    ):
                        if mode == "custom":
                            yield payload
                            continue
                        if mode == "values":
                            state = payload
                            continue
                        for delta in payload.values():
                            for ev in (delta or {}).get("events", []):
                                yield ev
                    final = with_metadata(
                        state["final"],
                        state,
                        version="v2",
                        thread_id=thread_id,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        data_month=rt.deps.data_month,
                    )
                    slots = next_slots(state)
            rt.sessions.append(thread_id, message, final["reply"], slots)
            yield {"type": "FinalAnswer", **final}
        except Exception as e:  # 계약상 스트림은 Error 이벤트로 끝나야 한다 — 원인은 메시지에 남긴다
            yield {
                "type": "Error",
                "code": error_code(e),
                "message": f"{type(e).__name__}: {e}"[:300],
            }

    @app.post("/chat")
    async def chat(req: ChatRequest):
        """POST /chat: 이벤트를 끝까지 받아 마지막 FinalAnswer 또는 Error를 JSON으로 돌려준다."""
        if err := request_error(req.message, req.thread_id):
            return JSONResponse({"code": "BAD_REQUEST", "message": err}, 400)
        last = None
        async for ev in events(req.message, req.thread_id):
            last = ev
        body = {k: v for k, v in last.items() if k != "type"}
        return JSONResponse(
            body,
            status_code=200
            if last["type"] == "FinalAnswer"
            else (503 if last["code"] == "LLM_UNAVAILABLE" else 500),
        )

    @app.get("/chat/stream")
    async def chat_stream(message: str, thread_id: str = "default"):
        """GET /chat/stream: 진행 이벤트를 SSE로 보내고 [DONE]으로 끝낸다."""

        async def gen():
            """입력을 검증한 뒤 이벤트를 SSE 문자열로 내보낸다."""
            if err := request_error(message, thread_id):
                yield _sse(
                    {"type": "Error", "code": "BAD_REQUEST", "message": err}
                )
            else:
                async for ev in events(message, thread_id):
                    yield _sse(ev)
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _sse(ev: dict) -> str:
    """이벤트 dict를 SSE 'data:' 한 건으로 만든다."""
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8100, log_level="warning")
