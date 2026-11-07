from fastapi.testclient import TestClient

from app.main import app

CSRF_HEADERS = {"X-Requested-With": "fetch", "Origin": "https://testserver"}


def test_health() -> None:
    assert TestClient(app).get("/api/health").json() == {"status": "ok"}


def test_post_message_streams_trd_events_in_order(web_client: TestClient) -> None:
    with web_client.stream(
        "POST", "/api/conversations/test/messages", json={"text": "안녕"}
    ) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = [
            line.split(":", 1)[1].strip() for line in res.iter_lines() if line.startswith("event:")
        ]

    assert events[0] == "status"
    assert events[1] == "sources"
    assert set(events[2:-1]) == {"token"}
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
