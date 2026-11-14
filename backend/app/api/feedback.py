"""답변 피드백 API (F-12, TRD 5.3, 8.3).

`PUT /messages/{id}/feedback`: 좋아요(+1)·싫어요(-1), 사유(`wrong`, `not_found_wrong`, `other`,
없음), 메모. 답변당 1개이며 다시 보내면 덮어쓴다. 검토 때 붙이는 `failure_type`·`reviewed_at`은
건드리지 않는다.
로그 `feedback.saved`(메시지 ID, 평가, 사유, 메모 유무). 메모 원문은 남기지 않는다.

검토(평가 화면, PRD 8장 실패 사례 기록):
- `GET /feedback?rating=-1`: 피드백 목록(검토 안 한 것 먼저, 최신순)과 질문·답변.
- `PATCH /feedback/{id}`: 실패 유형(`parse`, `chunk`, `retrieval`, `rerank`, `hallucination`,
  null이면 해제)을 붙이고 `reviewed_at`을 지금으로 바꾼다. 로그 `feedback.reviewed`.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from app.api.deps import DbSession
from app.api.errors import ApiError
from app.core.logging import get_logger
from app.db.enums import FailureType, FeedbackReason, MessageRole
from app.db.models import Feedback, Message

router = APIRouter(tags=["feedback"])
log = get_logger(__name__)

NOTE_MAX = 2000


class FeedbackIn(BaseModel):
    rating: Literal[1, -1]
    reason: FeedbackReason | None = None
    note: str | None = Field(default=None, max_length=NOTE_MAX)

    @field_validator("note")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class FeedbackSaved(BaseModel):
    message_id: int
    rating: int
    reason: FeedbackReason | None
    note: str | None


@router.put("/messages/{message_id}/feedback")
async def put_feedback(message_id: int, body: FeedbackIn, db: DbSession) -> FeedbackSaved:
    role = await db.scalar(select(Message.role).where(Message.id == message_id))
    if role != MessageRole.ASSISTANT:
        raise ApiError(404, "message_not_found", "답변을 찾을 수 없습니다")
    values = {"rating": body.rating, "reason": body.reason, "note": body.note}
    stmt = insert(Feedback).values(message_id=message_id, **values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Feedback.message_id],
        set_={**values, "updated_at": func.now()},
    )
    await db.execute(stmt)
    await db.commit()
    log.info(
        "feedback.saved",
        message_id=message_id,
        rating=body.rating,
        reason=body.reason.value if body.reason else None,
        has_note=body.note is not None,
    )
    return FeedbackSaved(
        message_id=message_id, rating=body.rating, reason=body.reason, note=body.note
    )


# ---------------------------------------------------------------------------
# 검토(실패 유형 붙이기)
# ---------------------------------------------------------------------------


class FeedbackItem(BaseModel):
    id: int
    message_id: int
    conversation_id: int
    rating: int
    reason: FeedbackReason | None
    note: str | None
    failure_type: FailureType | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    question: str | None
    answer: str


class FeedbackList(BaseModel):
    items: list[FeedbackItem]
    total: int
    unreviewed: int


class FeedbackReviewIn(BaseModel):
    failure_type: FailureType | None


@router.get("/feedback")
async def list_feedback(db: DbSession, rating: int | None = None, limit: int = 100) -> FeedbackList:
    if rating is not None and rating not in (1, -1):
        raise ApiError(422, "invalid_rating", "rating은 1 또는 -1입니다")
    limit = max(1, min(limit, 500))
    answer = aliased(Message)
    question_id = (
        select(func.max(Message.id))
        .where(
            Message.conversation_id == answer.conversation_id,
            Message.id < answer.id,
            Message.role == MessageRole.USER,
        )
        .correlate(answer)
        .scalar_subquery()
    )
    question = aliased(Message)
    stmt = (
        select(Feedback, answer.conversation_id, answer.content, question.content)
        .join(answer, answer.id == Feedback.message_id)
        .outerjoin(question, question.id == question_id)
    )
    if rating is not None:
        stmt = stmt.where(Feedback.rating == rating)
    rows = (
        await db.execute(
            stmt.order_by(
                Feedback.reviewed_at.is_not(None), Feedback.updated_at.desc(), Feedback.id.desc()
            ).limit(limit)
        )
    ).all()
    count_stmt = select(func.count(), func.count().filter(Feedback.reviewed_at.is_(None)))
    if rating is not None:
        count_stmt = count_stmt.where(Feedback.rating == rating)
    total, unreviewed = (await db.execute(count_stmt.select_from(Feedback))).one()
    items = [
        FeedbackItem(
            id=fb.id,
            message_id=fb.message_id,
            conversation_id=conversation_id,
            rating=fb.rating,
            reason=fb.reason,
            note=fb.note,
            failure_type=fb.failure_type,
            reviewed_at=fb.reviewed_at,
            created_at=fb.created_at,
            updated_at=fb.updated_at,
            question=q,
            answer=a,
        )
        for fb, conversation_id, a, q in rows
    ]
    log.info("feedback.listed", rating=rating, count=len(items), unreviewed=unreviewed)
    return FeedbackList(items=items, total=total, unreviewed=unreviewed)


class FeedbackReviewed(BaseModel):
    id: int
    message_id: int
    failure_type: FailureType | None
    reviewed_at: datetime


@router.patch("/feedback/{feedback_id}")
async def review_feedback(
    feedback_id: int, body: FeedbackReviewIn, db: DbSession
) -> FeedbackReviewed:
    fb = await db.get(Feedback, feedback_id)
    if fb is None:
        raise ApiError(404, "feedback_not_found", "피드백을 찾을 수 없습니다")
    previous = fb.failure_type
    fb.failure_type = body.failure_type
    fb.reviewed_at = await db.scalar(select(func.now()))
    await db.commit()
    log.info(
        "feedback.reviewed",
        feedback_id=fb.id,
        message_id=fb.message_id,
        rating=fb.rating,
        failure_type=body.failure_type.value if body.failure_type else None,
        previous=previous.value if previous else None,
    )
    assert fb.reviewed_at is not None
    return FeedbackReviewed(
        id=fb.id,
        message_id=fb.message_id,
        failure_type=fb.failure_type,
        reviewed_at=fb.reviewed_at,
    )
