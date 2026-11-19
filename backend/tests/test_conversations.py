"""대화 API (F-11, TRD 5.3, 8.3).

테스트 DB 세션(끝나면 롤백) + Fake Provider로 실제 질의 그래프를 거쳐 메시지·질의 로그를 만든다.
"""

from typing import Any

import httpx
from chat_seed import parse_sse, seed_corpus
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.db.models import Conversation, Feedback, Message, QueryLog


async def ask(client: httpx.AsyncClient, text: str, conversation: str | int = "new") -> int:
    res = await client.post(f"/api/conversations/{conversation}/messages", json={"text": text})
    assert res.status_code == 200, res.text
    done = [d for n, d in parse_sse(res.text) if n == "done"]
    assert done, res.text
    return int(done[0]["conversation_id"])


async def count(session: AsyncSession, model: Any, *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def test_crud_and_cascade_delete(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed_corpus(session)

    # 생성(빈 대화) → 목록
    with capture_logs() as logs:
        res = await chat_client.post("/api/conversations", json={})
    assert res.status_code == 201
    empty_id = res.json()["id"]
    assert res.json()["title"] == ""
    assert any(
        r["event"] == "conversation.created" and r["conversation_id"] == empty_id for r in logs
    )

    # 질문으로 만든 대화: 메시지 2쌍, 피드백 1개
    conv_id = await ask(chat_client, "알파 프로젝트 마감일 알려줘")
    await ask(chat_client, "위약금은 얼마야", conv_id)
    listed = (await chat_client.get("/api/conversations")).json()
    assert [c["id"] for c in listed][:2] == [conv_id, empty_id]  # 최근에 바뀐 순

    detail = (await chat_client.get(f"/api/conversations/{conv_id}")).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    answer = detail["messages"][1]
    assert answer["status"] == "complete"
    assert answer["citations"] and answer["citations"][0]["path"].endswith("알파.md")
    assert answer["feedback"] is None
    res = await chat_client.put(f"/api/messages/{answer['id']}/feedback", json={"rating": 1})
    assert res.status_code == 200
    assert (await chat_client.get(f"/api/conversations/{conv_id}")).json()["messages"][1][
        "feedback"
    ] == {"rating": 1, "reason": None, "note": None}

    # 이름 변경(앞뒤 공백 정리, 빈 제목 거절)
    with capture_logs() as logs:
        res = await chat_client.patch(f"/api/conversations/{conv_id}", json={"title": "  새 이름 "})
    assert res.status_code == 200
    assert res.json()["title"] == "새 이름"
    renamed = [r for r in logs if r["event"] == "conversation.renamed"]
    assert renamed and renamed[0]["conversation_id"] == conv_id
    assert "새 이름" not in str(renamed)  # 제목 원문은 로그에 남기지 않는다
    bad = await chat_client.patch(f"/api/conversations/{conv_id}", json={"title": "   "})
    assert bad.status_code == 422
    assert (await chat_client.get(f"/api/conversations/{conv_id}")).json()["title"] == "새 이름"

    # 없는 대화·숫자가 아닌 ID는 404
    for path in ("/api/conversations/999999999", "/api/conversations/abc"):
        assert (await chat_client.get(path)).status_code == 404
        assert (await chat_client.patch(path, json={"title": "x"})).status_code == 404
        assert (await chat_client.delete(path)).status_code == 404

    # 삭제: 메시지·질의 로그·피드백도 함께
    msg_ids = list(
        (await session.scalars(select(Message.id).where(Message.conversation_id == conv_id))).all()
    )
    assert await count(session, Message, Message.conversation_id == conv_id) == 4
    assert await count(session, QueryLog, QueryLog.message_id.in_(msg_ids)) == 2
    assert await count(session, Feedback, Feedback.message_id.in_(msg_ids)) == 1
    with capture_logs() as logs:
        res = await chat_client.delete(f"/api/conversations/{conv_id}")
    assert res.status_code == 204
    deleted = [r for r in logs if r["event"] == "conversation.deleted"]
    assert deleted == [
        {
            "event": "conversation.deleted",
            "log_level": "info",
            "conversation_id": conv_id,
            "messages": 4,
            "query_logs": 2,
            "feedback": 1,
        }
    ]
    assert await count(session, Conversation, Conversation.id == conv_id) == 0
    assert await count(session, Message, Message.conversation_id == conv_id) == 0
    assert await count(session, QueryLog, QueryLog.message_id.in_(msg_ids)) == 0
    assert await count(session, Feedback, Feedback.message_id.in_(msg_ids)) == 0
    assert (await chat_client.get(f"/api/conversations/{conv_id}")).status_code == 404
    assert [c["id"] for c in (await chat_client.get("/api/conversations")).json()] == [empty_id]

    # 다른 대화는 그대로
    assert await count(session, Conversation, Conversation.id == empty_id) == 1


async def test_title_from_first_question(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed_corpus(session)
    first = (
        "알파 프로젝트의   마감일과\n담당자를 한 번에 정리해서 알려 주세요. "
        "가능하면 자세하게 부탁합니다"
    )
    expected = "알파 프로젝트의 마감일과 담당자를 한 번에 정리해서 알려 주세요. 가능하"
    assert len(expected) == 40

    with capture_logs() as logs:
        conv_id = await ask(chat_client, first)
    created = [r for r in logs if r["event"] == "conversation.created"]
    assert created and created[0]["conversation_id"] == conv_id
    assert (await chat_client.get(f"/api/conversations/{conv_id}")).json()["title"] == expected

    # 두 번째 질문은 제목을 바꾸지 않는다
    await ask(chat_client, "위약금은 얼마야", conv_id)
    assert (await chat_client.get(f"/api/conversations/{conv_id}")).json()["title"] == expected

    # 제목 없이 만든 빈 대화는 첫 질문으로 제목을 채운다
    empty = (await chat_client.post("/api/conversations", json={})).json()
    await ask(chat_client, "  주간 회의는 언제 해?  ", empty["id"])
    assert (await chat_client.get(f"/api/conversations/{empty['id']}")).json()[
        "title"
    ] == "주간 회의는 언제 해?"

    # 짧은 질문은 그대로
    short_id = await ask(chat_client, "점심 메뉴")
    assert (await chat_client.get(f"/api/conversations/{short_id}")).json()["title"] == "점심 메뉴"
