import httpx
from chat_seed import seed_doc
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app

CSRF_HEADERS = {"X-Requested-With": "fetch", "Origin": "https://testserver"}


def test_health() -> None:
    # 공개 요약: 프로세스가 살아 있으면 항상 200, status "ok". 이상은 degraded·이상 코드만 알리고
    # 세부(checks: 모델 이름, 디스크 크기 등)는 세션이 필요한 /health/details에서만 준다
    client = TestClient(app)
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert set(body) == {"status", "degraded", "issues"}
    assert isinstance(body["degraded"], bool)
    assert all(set(i) == {"code"} for i in body["issues"])
    assert client.get("/api/health/details").status_code == 401


async def test_post_message_streams_trd_events_in_order(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """일반 답변: status(여러 번) → sources → token(여러 번) → done (TRD 7.3)."""
    await seed_doc(session, r"C:\docs\인사.md", ["안녕 인사말은 반갑습니다 이다."])
    res = await chat_client.post("/api/conversations/new/messages", json={"text": "안녕 인사말"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    events = [
        line.split(":", 1)[1].strip() for line in res.text.splitlines() if line.startswith("event:")
    ]

    first_sources = events.index("sources")
    assert events[0] == "status"
    assert set(events[:first_sources]) == {"status"}
    assert set(events[first_sources + 1 : -1]) == {"token"}
    assert events[-1] == "done"


def test_post_message_rejects_empty_text(web_client: TestClient) -> None:
    res = web_client.post("/api/conversations/test/messages", json={"text": ""})
    assert res.status_code == 422


def test_post_message_requires_session() -> None:
    """세션 쿠키가 없으면 401 `unauthenticated`(TRD 8.3). 웹은 이때 로그인 화면으로 보낸다."""
    client = TestClient(app, base_url="https://testserver", headers=CSRF_HEADERS)
    res = client.post("/api/conversations/test/messages", json={"text": "안녕"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthenticated"
