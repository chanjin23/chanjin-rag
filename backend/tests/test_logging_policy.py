"""로그 정책: 질문 원문은 기본적으로 앱 로그에 남기지 않는다 (TRD 11장, 12.1 LOG_QUERY_TEXT)."""

from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from app.core.config import get_settings

QUESTION = "작년 계약서의 위약금 조항 알려줘"


async def _received_events(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """새 대화로 질문을 보내고, 그 요청에서 남은 로그 전체(질의 그래프 포함)를 돌려준다."""
    with capture_logs() as logs:
        res = await client.post("/api/conversations/new/messages", json={"text": QUESTION})
        assert res.status_code == 200
    return logs


async def test_query_text_not_logged_by_default(
    monkeypatch: pytest.MonkeyPatch, chat_client: httpx.AsyncClient
) -> None:
    monkeypatch.delenv("LOG_QUERY_TEXT", raising=False)
    assert get_settings().log_query_text is False

    logs = await _received_events(chat_client)
    (event,) = [e for e in logs if e["event"] == "chat.message.received"]
    assert isinstance(event["conversation_id"], int)
    assert event["length"] == len(QUESTION)
    assert "text" not in event
    assert QUESTION not in repr(event)
    # 질의 그래프·검색·완료 로그에도 질문 원문이 없다
    assert any(e["event"].startswith("chat.message.") and e is not event for e in logs)
    assert QUESTION not in repr(logs)


async def test_query_text_logged_when_enabled(
    monkeypatch: pytest.MonkeyPatch, chat_client: httpx.AsyncClient
) -> None:
    monkeypatch.setenv("LOG_QUERY_TEXT", "true")
    assert get_settings().log_query_text is True

    logs = await _received_events(chat_client)
    (event,) = [e for e in logs if e["event"] == "chat.message.received"]
    assert event["text"] == QUESTION
    assert event["length"] == len(QUESTION)
