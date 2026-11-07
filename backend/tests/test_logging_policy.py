"""로그 정책: 질문 원문은 기본적으로 앱 로그에 남기지 않는다 (TRD 11장, 12.1 LOG_QUERY_TEXT)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from app.core.config import get_settings

QUESTION = "작년 계약서의 위약금 조항 알려줘"


def _received_events(client: TestClient) -> list[dict[str, Any]]:
    with capture_logs() as logs:
        with client.stream(
            "POST", "/api/conversations/c1/messages", json={"text": QUESTION}
        ) as res:
            assert res.status_code == 200
            res.read()
    return [e for e in logs if e["event"] == "chat.message.received"]


def test_query_text_not_logged_by_default(
    monkeypatch: pytest.MonkeyPatch, web_client: TestClient
) -> None:
    monkeypatch.delenv("LOG_QUERY_TEXT", raising=False)
    assert get_settings().log_query_text is False

    (event,) = _received_events(web_client)
    assert event["conversation_id"] == "c1"
    assert event["length"] == len(QUESTION)
    assert "text" not in event
    assert QUESTION not in repr(event)


def test_query_text_logged_when_enabled(
    monkeypatch: pytest.MonkeyPatch, web_client: TestClient
) -> None:
    monkeypatch.setenv("LOG_QUERY_TEXT", "true")
    assert get_settings().log_query_text is True

    (event,) = _received_events(web_client)
    assert event["text"] == QUESTION
    assert event["length"] == len(QUESTION)
