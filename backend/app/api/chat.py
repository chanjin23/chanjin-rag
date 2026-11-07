"""질문 API. POST /api/conversations/{id}/messages → SSE 응답 (TRD 7.3, 8.3).

현재 단계: 질문을 받아 로그에 남기고, 질의 그래프 대신
받은 내용을 그대로 돌려주는 스텁 스트림을 보낸다.
이벤트 이름과 순서(status → sources → token → done)는 TRD 7.3과 같게 맞춰,
이후 LangGraph로 바꿔도 프론트엔드를 고치지 않아도 되게 한다.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.core.logging import get_logger

router = APIRouter(tags=["chat"])
log = get_logger(__name__)


class SearchFilters(BaseModel):
    directory: str | None = None
    exts: list[str] | None = None
    modified_from: date | None = None
    modified_to: date | None = None


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    filters: SearchFilters | None = None
    summarize_document_id: int | None = None


def _event(name: str, data: object) -> dict[str, str]:
    return {"event": name, "data": json.dumps(data, ensure_ascii=False)}


@router.post("/conversations/{conversation_id}/messages")
async def post_message(conversation_id: str, body: MessageIn) -> EventSourceResponse:
    log.info(
        "chat.message.received",
        conversation_id=conversation_id,
        text=body.text,
        length=len(body.text),
        filters=body.filters.model_dump(exclude_none=True) if body.filters else None,
    )
    started = time.perf_counter()

    async def stream() -> AsyncIterator[dict[str, str]]:
        yield _event("status", {"stage": "generating"})
        yield _event("sources", [])
        reply = f"백엔드가 질문을 받았습니다: {body.text}"
        for ch in reply:
            yield _event("token", {"text": ch})
            await asyncio.sleep(0.01)
        latency_ms = round((time.perf_counter() - started) * 1000)
        yield _event("done", {"message_id": None, "not_found": False, "latency_ms": latency_ms})
        log.info("chat.message.completed", conversation_id=conversation_id, latency_ms=latency_ms)

    return EventSourceResponse(stream(), headers={"X-Accel-Buffering": "no"})
