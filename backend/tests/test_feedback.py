"""답변 피드백 API (F-12, TRD 5.3, 8.3)와 색인 범위 API (F-18)."""

from datetime import date

import httpx
from chat_seed import parse_sse, seed_corpus, seed_doc
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.api.index import estimate_completion
from app.db.enums import DocumentStatus, FailureType, FeedbackReason
from app.db.models import Feedback, Message


async def _answer_id(client: httpx.AsyncClient) -> int:
    res = await client.post("/api/conversations/new/messages", json={"text": "위약금은 얼마야"})
    done = [d for n, d in parse_sse(res.text) if n == "done"]
    return int(done[0]["message_id"])


async def test_feedback_upsert_with_reason(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed_corpus(session)
    message_id = await _answer_id(chat_client)
    url = f"/api/messages/{message_id}/feedback"

    # 좋아요
    with capture_logs() as logs:
        res = await chat_client.put(url, json={"rating": 1})
    assert res.status_code == 200
    assert res.json() == {"message_id": message_id, "rating": 1, "reason": None, "note": None}
    saved = [r for r in logs if r["event"] == "feedback.saved"]
    assert saved == [
        {
            "event": "feedback.saved",
            "log_level": "info",
            "message_id": message_id,
            "rating": 1,
            "reason": None,
            "has_note": False,
        }
    ]

    # 검토 때 붙인 실패 유형은 덮어쓰기에도 남는다
    await session.execute(
        update(Feedback)
        .where(Feedback.message_id == message_id)
        .values(failure_type=FailureType.RETRIEVAL)
    )

    # 다시 보내면 덮어쓴다(싫어요 + 사유 + 메모). 메모 원문은 로그에 남기지 않는다
    with capture_logs() as logs:
        res = await chat_client.put(
            url, json={"rating": -1, "reason": "not_found_wrong", "note": "  2쪽에 있음  "}
        )
    assert res.status_code == 200
    assert res.json()["reason"] == "not_found_wrong"
    assert res.json()["note"] == "2쪽에 있음"
    saved = [r for r in logs if r["event"] == "feedback.saved"]
    assert saved[0]["rating"] == -1
    assert saved[0]["reason"] == "not_found_wrong"
    assert saved[0]["has_note"] is True
    assert "2쪽에 있음" not in str(logs)

    rows = list(
        (await session.scalars(select(Feedback).where(Feedback.message_id == message_id))).all()
    )
    assert len(rows) == 1
    await session.refresh(rows[0])
    fb = rows[0]
    assert (fb.rating, fb.reason, fb.note) == (-1, FeedbackReason.NOT_FOUND_WRONG, "2쪽에 있음")
    assert fb.failure_type == FailureType.RETRIEVAL

    # 사유 바꾸기, 빈 메모는 없음으로
    res = await chat_client.put(url, json={"rating": -1, "reason": "wrong", "note": "   "})
    assert res.json() == {"message_id": message_id, "rating": -1, "reason": "wrong", "note": None}
    res = await chat_client.put(
        url, json={"rating": -1, "reason": "other", "note": "출처가 이상함"}
    )
    assert res.json()["reason"] == "other"

    # 잘못된 값은 422, 사용자 메시지·없는 메시지는 404
    assert (await chat_client.put(url, json={"rating": 0})).status_code == 422
    assert (await chat_client.put(url, json={"rating": 1, "reason": "nope"})).status_code == 422
    assert (await chat_client.put(url, json={"rating": 1, "note": "x" * 2001})).status_code == 422
    user_msg = await session.scalar(
        select(Message.id).where(Message.id < message_id).order_by(Message.id.desc()).limit(1)
    )
    res = await chat_client.put(f"/api/messages/{user_msg}/feedback", json={"rating": 1})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "message_not_found"
    assert (
        await chat_client.put("/api/messages/999999999/feedback", json={"rating": 1})
    ).status_code == 404

    # 대화 조회에 마지막 피드백이 보인다
    await session.refresh(fb)
    conv = await session.scalar(select(Message.conversation_id).where(Message.id == message_id))
    detail = (await chat_client.get(f"/api/conversations/{conv}")).json()
    assert detail["messages"][1]["feedback"] == {
        "rating": -1,
        "reason": "other",
        "note": "출처가 이상함",
    }


async def test_index_coverage(chat_client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed_corpus(session)  # 임베딩까지 끝난 문서 4개
    await seed_doc(
        session, r"C:\docs\대기.md", ["아직 임베딩 전"], status=DocumentStatus.INDEXED, embed=False
    )
    await seed_doc(session, r"C:\docs\큐.md", status=DocumentStatus.QUEUED, chunks=False)

    with capture_logs() as logs:
        res = await chat_client.get("/api/index/coverage")
    assert res.status_code == 200
    body = res.json()
    assert body["total_docs"] == 6
    assert body["bm25_docs"] == 5
    assert body["vector_docs"] == 4
    assert body["by_status"]["ready"] == 4
    assert body["by_status"]["indexed"] == 1
    assert body["by_status"]["queued"] == 1
    assert body["embed_waiting"] == 2
    assert body["daily_limit"] == 100
    assert body["estimated_completion"] is not None
    assert any(r["event"] == "index.coverage.read" for r in logs)


def test_estimate_completion() -> None:
    today = date(2026, 11, 10)
    assert estimate_completion(0, today=today, limit=100, used_today=0) is None
    assert estimate_completion(30, today=today, limit=100, used_today=50) == today
    assert estimate_completion(51, today=today, limit=100, used_today=50) == date(2026, 11, 11)
    assert estimate_completion(250, today=today, limit=100, used_today=100) == date(2026, 11, 13)
    assert estimate_completion(1000, today=today, limit=100, used_today=0) == date(2026, 11, 19)
