"""대화 API (F-11, TRD 5.3, 8.3).

- `GET /conversations`: 목록(최근에 바뀐 순).
- `POST /conversations`: 빈 대화 만들기(제목을 주지 않으면 빈 제목. 첫 질문의 앞 40자로 채운다).
- `GET /conversations/{id}`: 대화와 메시지(저장된 인용·피드백·찾지 못함 여부 포함).
- `PATCH /conversations/{id}`: 제목 바꾸기.
- `DELETE /conversations/{id}`: 삭제. 메시지·질의 로그·피드백도 함께 지운다(ON DELETE CASCADE).

웹은 보통 `POST /conversations/new/messages`(첫 질문)로 대화를 만든다(`app.api.chat`).
로그: `conversation.created`, `conversation.renamed`, `conversation.deleted`.
제목 원문은 남기지 않는다(질문 앞부분이므로, TRD 11장).
"""

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession
from app.api.errors import ApiError
from app.core.logging import get_logger
from app.db.models import Conversation, Feedback, Message, QueryLog

router = APIRouter(tags=["conversations"])
log = get_logger(__name__)

TITLE_CHARS = 40
TITLE_MAX = 200


def make_title(text: str) -> str:
    """첫 질문 앞 40자(공백은 한 칸으로)."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:TITLE_CHARS]


async def create_conversation(db: AsyncSession, title: str, *, source: str) -> Conversation:
    conv = Conversation(title=title)
    db.add(conv)
    await db.flush()
    log.info("conversation.created", conversation_id=conv.id, source=source)
    return conv


def parse_conversation_id(raw: str | int) -> int:
    value = str(raw)
    if not value.isdigit():
        raise ApiError(404, "conversation_not_found", "대화를 찾을 수 없습니다")
    return int(value)


async def get_conversation(
    db: AsyncSession, conversation_id: int, *, lock: bool = False
) -> Conversation:
    stmt = select(Conversation).where(Conversation.id == conversation_id)
    if lock:
        stmt = stmt.with_for_update()
    found = (await db.execute(stmt)).scalar_one_or_none()
    if found is None:
        raise ApiError(404, "conversation_not_found", "대화를 찾을 수 없습니다")
    return found


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------


class ConversationOut(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class FeedbackOut(BaseModel):
    rating: int
    reason: str | None
    note: str | None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    status: str
    citations: list[dict[str, Any]]
    created_at: datetime
    not_found: bool = False
    coverage: dict[str, Any] | None = None
    feedback: FeedbackOut | None = None


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=TITLE_MAX)


class ConversationPatch(BaseModel):
    title: str = Field(max_length=TITLE_MAX)

    @field_validator("title")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = re.sub(r"\s+", " ", v).strip()
        if not v:
            raise ValueError("제목을 입력하세요")
        return v


def _out(conv: Conversation) -> ConversationOut:
    return ConversationOut(
        id=conv.id, title=conv.title, created_at=conv.created_at, updated_at=conv.updated_at
    )


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------


@router.get("/conversations")
async def list_conversations(db: DbSession) -> list[ConversationOut]:
    rows = (
        await db.execute(
            select(Conversation).order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        )
    ).scalars()
    return [_out(c) for c in rows]


@router.post("/conversations", status_code=201)
async def create(body: ConversationCreate, db: DbSession) -> ConversationOut:
    title = re.sub(r"\s+", " ", body.title or "").strip()
    conv = await create_conversation(db, title, source="api")
    await db.commit()
    await db.refresh(conv)
    return _out(conv)


@router.get("/conversations/{conversation_id}")
async def read(conversation_id: str, db: DbSession) -> ConversationDetail:
    conv = await get_conversation(db, parse_conversation_id(conversation_id))
    rows = (
        await db.execute(
            select(Message, Feedback, QueryLog.not_found, QueryLog.coverage)
            .outerjoin(Feedback, Feedback.message_id == Message.id)
            .outerjoin(QueryLog, QueryLog.message_id == Message.id)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.id)
        )
    ).all()
    messages: list[MessageOut] = []
    seen: set[int] = set()
    for m, fb, not_found, coverage in rows:
        if m.id in seen:  # 질의 로그가 여러 개인 경우(정상 흐름에서는 없음)
            continue
        seen.add(m.id)
        messages.append(
            MessageOut(
                id=m.id,
                role=m.role.value,
                content=m.content,
                status=m.status.value,
                citations=m.citations or [],
                created_at=m.created_at,
                not_found=bool(not_found),
                coverage=coverage if not_found else None,
                feedback=(
                    FeedbackOut(
                        rating=fb.rating,
                        reason=fb.reason.value if fb.reason else None,
                        note=fb.note,
                    )
                    if fb is not None
                    else None
                ),
            )
        )
    return ConversationDetail(**_out(conv).model_dump(), messages=messages)


@router.patch("/conversations/{conversation_id}")
async def rename(conversation_id: str, body: ConversationPatch, db: DbSession) -> ConversationOut:
    conv = await get_conversation(db, parse_conversation_id(conversation_id), lock=True)
    conv.title = body.title
    await db.commit()
    await db.refresh(conv)
    log.info("conversation.renamed", conversation_id=conv.id, title_chars=len(body.title))
    return _out(conv)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def remove(conversation_id: str, db: DbSession) -> Response:
    conv = await get_conversation(db, parse_conversation_id(conversation_id), lock=True)
    message_ids = select(Message.id).where(Message.conversation_id == conv.id)
    counts = (
        await db.execute(
            select(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conv.id)
                .scalar_subquery(),
                select(func.count())
                .select_from(QueryLog)
                .where(QueryLog.message_id.in_(message_ids))
                .scalar_subquery(),
                select(func.count())
                .select_from(Feedback)
                .where(Feedback.message_id.in_(message_ids))
                .scalar_subquery(),
            )
        )
    ).one()
    # 메시지·질의 로그·피드백은 외래 키 ON DELETE CASCADE로 함께 지워진다(TRD 5.3)
    await db.execute(delete(Conversation).where(Conversation.id == conv.id))
    await db.commit()
    log.info(
        "conversation.deleted",
        conversation_id=conv.id,
        messages=int(counts[0]),
        query_logs=int(counts[1]),
        feedback=int(counts[2]),
    )
    return Response(status_code=204)
