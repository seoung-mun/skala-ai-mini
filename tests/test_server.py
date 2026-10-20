"""서버 계약 테스트: 실제 SSE 응답이 doc/API_CONTRACT.md / doc/contract/*.sse 규칙을 지키는지."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from finagent.search import KeywordIndex
from finagent.server import Runtime, create_app
from finagent.session_store import SqliteSessionStore
from test_graph import CAT, FakeLLM, FakeMCP, FakeRetriever, A
from finagent.deps import Deps

ROOT = Path(__file__).resolve().parents[1]
REQ = {"FinalAnswer": {"reply", "route", "products", "citations", "calculations", "warnings", "metadata"},
       "Error": {"code", "message"}, "RouteDecided": {"route", "reason"}, "PlanCreated": {"tasks"},
       "TaskCompleted": {"id", "agent", "ok", "retries", "elapsed_ms"},
       "ReplanCheck": {"round", "is_sufficient", "reason"}, "Validation": {"attempt", "verdict", "reason"}}
META = {"version", "latency_ms", "llm_calls", "retries", "validation_attempts", "cost_usd", "data_month", "thread_id"}


def parse_sse(text: str) -> list:
    chunks = [c[6:] for c in text.split("\n\n") if c.startswith("data: ")]
    assert chunks[-1] == "[DONE]"
    return [json.loads(c) for c in chunks[:-1]]


def check_contract(evs: list[dict]) -> None:
    ends = [e for e in evs if e["type"] in ("FinalAnswer", "Error")]
    assert len(ends) == 1 and evs[-1] is ends[0], [e["type"] for e in evs]
    for e in evs:
        assert REQ[e["type"]] <= e.keys(), (e["type"], REQ[e["type"]] - e.keys())
    if evs[-1]["type"] == "FinalAnswer":
        assert META <= evs[-1]["metadata"].keys() and evs[-1]["warnings"]


def test_contract_fixtures_follow_the_same_rules():
    for f in sorted((ROOT / "doc/contract").glob("*.sse")):
        check_contract(parse_sse(f.read_text()))


def make_app(llm, version="v2", tmp=":memory:"):
    async def rt():
        deps = Deps(llm=llm, mcp=FakeMCP(), retriever=FakeRetriever([A]), catalog=CAT, data_month="202609")
        idx = KeywordIndex({A: "가나적금 우대 0.5%p"})
        return Runtime(version, deps, idx, SqliteSessionStore(tmp), "fake")
    return create_app(rt)


def test_stream_v2_easy_follows_contract_and_remembers_thread(tmp_path):
    ok = {"ok": True, "reply": "연 0.5%p예요", "citations": [], "missing": ""}
    with TestClient(make_app(FakeLLM(ok, ok), tmp=tmp_path / "s.db")) as c:
        evs = parse_sse(c.get("/chat/stream", params={"message": "가나적금 우대조건 뭐야?", "thread_id": "t"}).text)
        check_contract(evs)
        assert [e["type"] for e in evs] == ["RouteDecided", "TaskCompleted", "Validation", "FinalAnswer"]
        evs2 = parse_sse(c.get("/chat/stream", params={"message": "그거 가입 방법은?", "thread_id": "t"}).text)
        assert evs2[0]["route"] == "easy" and evs2[-1]["products"][0]["id"] == A  # 직전 상품 이어받음


def test_post_chat_returns_final_body_and_v1_toggle():
    with TestClient(make_app(FakeLLM("3.85%입니다"), version="v1")) as c:
        body = c.post("/chat", json={"message": "가나적금 금리"}).json()
        assert body["route"] == "baseline" and body["metadata"]["version"] == "v1" and body["reply"] == "3.85%입니다"
        assert c.get("/health").json()["version"] == "v1"


def test_errors_are_contract_errors():
    class Down:
        def structured(self, *a, **k):
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
    class NeverCalled:
        def structured(self, *a, **k):
            raise AssertionError("잘못된 입력이 LLM까지 갔다")
    with TestClient(make_app(NeverCalled())) as c:
        for body in ({"message": ""}, {"message": "   "}, {"message": "x" * 501},
                     {"message": "안녕", "thread_id": "x" * 101}, {"message": 3}, {}):
            r = c.post("/chat", json=body)
            assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST", body
        for params in ({"message": "   "}, {"message": "안녕", "thread_id": "x" * 101}):
            evs = parse_sse(c.get("/chat/stream", params=params).text)
            assert evs == [{"type": "Error", "code": "BAD_REQUEST", "message": evs[0]["message"]}], params
