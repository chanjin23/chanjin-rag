from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    assert client.get("/api/health").json() == {"status": "ok"}


def test_post_message_streams_trd_events_in_order() -> None:
    with client.stream("POST", "/api/conversations/test/messages", json={"text": "안녕"}) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = [
            line.split(":", 1)[1].strip() for line in res.iter_lines() if line.startswith("event:")
        ]

    assert events[0] == "status"
    assert events[1] == "sources"
    assert set(events[2:-1]) == {"token"}
    assert events[-1] == "done"


def test_post_message_rejects_empty_text() -> None:
    assert client.post("/api/conversations/test/messages", json={"text": ""}).status_code == 422
