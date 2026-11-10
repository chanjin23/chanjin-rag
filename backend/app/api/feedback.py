"""답변 피드백 API (F-12, TRD 5.3, 8.3).

`PUT /messages/{id}/feedback`: 좋아요(+1)·싫어요(-1), 사유(`wrong`, `not_found_wrong`, `other`,
없음), 메모. 답변당 1개이며 다시 보내면 덮어쓴다. 검토 때 붙이는 `failure_type`·`reviewed_at`은
건드리지 않는다.
로그 `feedback.saved`(메시지 ID, 평가, 사유, 메모 유무). 메모 원문은 남기지 않는다.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import DbSession
from app.api.errors import ApiError
from app.core.logging import get_logger
from app.db.enums import FeedbackReason, MessageRole
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
