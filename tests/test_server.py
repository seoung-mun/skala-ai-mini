"""서버 계약 테스트: 실제 SSE 응답이 doc/API_CONTRACT.md / doc/contract/*.sse 규칙을 지키는지."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
from test_graph import CAT, A, FakeLLM, FakeMCP, FakeRetriever

from finagent.deps import Deps
from finagent.search import KeywordIndex
from finagent.server import Runtime, create_app
from finagent.session_store import SqliteSessionStore

ROOT = Path(__file__).resolve().parents[1]
REQ = {
    "FinalAnswer": {
        "reply",
        "route",
        "products",
        "citations",
        "calculations",
        "warnings",
        "metadata",
    },
    "Error": {"code", "message"},
    "RouteDecided": {"route", "reason"},
    "PlanCreated": {"tasks"},
    "TaskCompleted": {"id", "agent", "ok", "retries", "elapsed_ms", "error"},
    "ReplanCheck": {"round", "is_sufficient", "reason"},
    "Validation": {"attempt", "verdict", "reason"},
    "AnswerProgress": {"stage", "attempt"},
}
META = {
    "version",
    "latency_ms",
    "llm_calls",
    "retries",
    "validation_attempts",
    "cost_usd",
    "data_month",
    "thread_id",
}


def parse_sse(text: str) -> list:
    """SSE 응답을 이벤트 목록으로 바꾼다([DONE]으로 끝나는지 확인)."""
    chunks = [c[6:] for c in text.split("\n\n") if c.startswith("data: ")]
    assert chunks[-1] == "[DONE]"
    return [json.loads(c) for c in chunks[:-1]]


def check_contract(evs: list[dict]) -> None:
    """이벤트 목록이 API 계약(끝 이벤트 1개, 이벤트별 필수 키)을 지키는지 검사한다."""
    ends = [e for e in evs if e["type"] in ("FinalAnswer", "Error")]
    assert len(ends) == 1 and evs[-1] is ends[0], [e["type"] for e in evs]
    for e in evs:
        assert REQ[e["type"]] <= e.keys(), (
            e["type"],
            REQ[e["type"]] - e.keys(),
        )
    if evs[-1]["type"] == "FinalAnswer":
        assert META <= evs[-1]["metadata"].keys() and evs[-1]["warnings"]


def test_contract_fixtures_follow_the_same_rules():
    """계약 예시 SSE 파일도 같은 규칙을 지킨다."""
    for f in sorted((ROOT / "doc/contract").glob("*.sse")):
        check_contract(parse_sse(f.read_text()))


def make_app(llm, version="v2", tmp=":memory:"):
    """가짜 Runtime으로 테스트용 앱을 만든다."""

    async def rt():
        """가짜 의존성과 메모리 세션 DB로 Runtime을 만든다."""
        deps = Deps(
            llm=llm,
            mcp=FakeMCP(),
            retriever=FakeRetriever([A]),
            catalog=CAT,
            data_month="202609",
            planner="rule",
            rag_agent="fixed",
        )
        idx = KeywordIndex({A: "가나적금 우대 0.5%p"})
        return Runtime(version, deps, idx, SqliteSessionStore(tmp), "fake")

    return create_app(rt)


def test_stream_v2_easy_follows_contract_and_remembers_thread(tmp_path):
    """v2 스트림이 계약을 지키고 같은 thread의 대화를 기억한다."""
    ok = {"ok": True, "reply": "연 0.5%p예요", "citations": [], "missing": ""}
    rewrite = {
        "follow_up": True,
        "question": "가나적금 가입 방법은?",
        "reason": "직전 상품",
    }  # 두 번째 턴은 기록이 있어 재작성
    with TestClient(
        make_app(
            FakeLLM(
                ok,
                rewrite,
                {**ok, "reply": "가은행 가나적금 가입 방법은 스마트폰입니다."},
            ),
            tmp=tmp_path / "s.db",
        )
    ) as c:
        evs = parse_sse(
            c.get(
                "/chat/stream",
                params={"message": "가나적금 우대조건 뭐야?", "thread_id": "t"},
            ).text
        )
        check_contract(evs)
        assert [e["type"] for e in evs] == [
            "RouteDecided",
            "TaskCompleted",
            "AnswerProgress",
            "AnswerProgress",
            "Validation",
            "FinalAnswer",
        ]
        progress = [e for e in evs if e["type"] == "AnswerProgress"]
        assert [e["stage"] for e in progress] == ["writing", "validating"]
        assert progress[1]["writing_ms"] >= 0
        assert all("reply" not in e for e in progress)
        evs2 = parse_sse(
            c.get(
                "/chat/stream",
                params={"message": "그거 가입 방법은?", "thread_id": "t"},
            ).text
        )
        assert (
            evs2[0]["route"] == "easy" and evs2[-1]["products"][0]["id"] == A
        )  # 직전 상품 이어받음
        assert evs2[0]["rewrite"] == {
            "original": "그거 가입 방법은?",
            "question": "가나적금 가입 방법은?",
            "applied": True,
            "reason": "직전 상품",
        }


def test_post_chat_returns_final_body_and_v1_toggle():
    """POST /chat은 최종 본문을 돌려주고 v1 전환도 동작한다."""
    with TestClient(make_app(FakeLLM("3.85%입니다"), version="v1")) as c:
        body = c.post("/chat", json={"message": "가나적금 금리"}).json()
        assert (
            body["route"] == "baseline"
            and body["metadata"]["version"] == "v1"
            and body["reply"] == "3.85%입니다"
        )
        assert c.get("/health").json()["version"] == "v1"


def test_errors_are_contract_errors():
    """LLM 연결 실패가 계약 오류(503 LLM_UNAVAILABLE)로 나온다."""

    class Down:
        """항상 연결 실패를 내는 가짜 LLM."""

        def structured(self, *a, **k):
            """연결 실패(ConnectError)를 낸다."""
            import httpx

            raise httpx.ConnectError("refused")

    with TestClient(make_app(Down())) as c:
        evs = parse_sse(c.get("/chat/stream", params={"message": "안녕"}).text)
        check_contract(evs)
        assert evs[-1]["code"] == "LLM_UNAVAILABLE"
        bad = parse_sse(c.get("/chat/stream", params={"message": " "}).text)
        assert bad[-1]["code"] == "BAD_REQUEST"
        assert c.post("/chat", json={"message": "안녕"}).status_code == 503


def test_input_validation_is_the_same_for_post_and_stream():
    """POST와 스트림이 같은 입력 검증을 하고 LLM까지 가지 않는다."""

    class NeverCalled:
        """호출되면 실패하는 가짜 LLM."""

        def structured(self, *a, **k):
            """호출되면 AssertionError를 낸다."""
            raise AssertionError("잘못된 입력이 LLM까지 갔다")

    with TestClient(make_app(NeverCalled())) as c:
        for body in (
            {"message": ""},
            {"message": "   "},
            {"message": "x" * 501},
            {"message": "안녕", "thread_id": "x" * 101},
            {"message": 3},
            {},
        ):
            r = c.post("/chat", json=body)
            assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST", (
                body
            )
        for params in (
            {"message": "   "},
            {"message": "안녕", "thread_id": "x" * 101},
        ):
            evs = parse_sse(c.get("/chat/stream", params=params).text)
            assert evs == [
                {
                    "type": "Error",
                    "code": "BAD_REQUEST",
                    "message": evs[0]["message"],
                }
            ], params


def test_reset_removes_history_and_slots_without_touching_other_threads():
    with TestClient(make_app(FakeLLM())) as client:
        store = client.app.state.rt.sessions
        slots = {"kinds": ["saving"], "term_months": 12}
        store.append("reset-me", "이전 질문", "이전 답변", slots)
        store.append("keep-me", "보존 질문", "보존 답변", slots)
        response = client.post(
            "/sessions/reset", json={"thread_id": "reset-me"}
        )
        assert response.status_code == 200 and response.json()["ok"]
        assert store.load("reset-me") == ([], None)
        assert store.load("keep-me")[0][0]["content"] == "보존 질문"
        assert (
            client.post(
                "/sessions/reset", json={"thread_id": "reset-me"}
            ).status_code
            == 200
        )
        for invalid in ["", "   ", "x" * 101]:
            assert (
                client.post(
                    "/sessions/reset", json={"thread_id": invalid}
                ).status_code
                == 400
            )
        recall = client.post(
            "/chat",
            json={
                "thread_id": "reset-me",
                "message": "내가 방금 뭐라고 질문했어?",
            },
        )
        assert "기록이 없어요" in recall.json()["reply"]


def test_reset_waits_for_running_turn_so_old_context_cannot_be_saved_again():
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout
    from threading import Event

    import pytest

    entered, release = Event(), Event()
    with (
        TestClient(make_app(FakeLLM())) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        rt = client.app.state.rt
        rt.sessions.append(
            "race", "예전 질문", "예전 답변", {"kinds": ["saving"]}
        )
        original = rt.graph.astream

        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.to_thread(release.wait, 2)
            async for event in original(*args, **kwargs):
                yield event

        rt.graph.astream = slow
        turn = pool.submit(
            client.post,
            "/chat",
            json={"thread_id": "race", "message": "내가 방금 뭐라고 질문했어?"},
        )
        assert entered.wait(2)
        reset = pool.submit(
            client.post, "/sessions/reset", json={"thread_id": "race"}
        )
        try:
            with pytest.raises(FutureTimeout):
                reset.result(timeout=0.1)
        finally:
            release.set()
        assert turn.result(timeout=3).status_code == 200
        assert reset.result(timeout=3).status_code == 200
        assert rt.sessions.load("race") == ([], None)
