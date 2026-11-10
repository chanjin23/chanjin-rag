"""질문 API. POST /api/conversations/{id}/messages → SSE 응답 (PRD 5.2, F-08, F-09, TRD 7.3, 8.3).

흐름:

1. 대화를 찾는다(`new`면 만든다. 제목은 질문 앞 40자). 대화 행을 `FOR UPDATE`로 잠가 같은 대화의
   동시 요청을 줄 세운다.
2. 2분 넘게 `streaming`인 메시지는 `aborted`로 정리하고, 그래도 `streaming`이 남아 있으면 SSE를
   시작하지 않고 409 `conflict`로 거절한다.
3. 사용자 메시지(`complete`)와 답변 메시지(`streaming`)를 저장하고 SSE를 시작한다.
4. 질의 그래프(`app.query.graph`)는 별도 작업(task)에서 돌리고 이벤트를 큐로 받아 보낸다.
   - 끝나면 답변을 `complete`로 저장하고 `done`을 보낸다.
   - 실패하면 그때까지 보낸 토큰을 `error`로 저장하고
     `error`(`code`, `message`, `retryable`)로 끝낸다.
   - 클라이언트가 연결을 끊으면 그래프 작업을 취소(생성 중단)하고
     그때까지의 답변을 `aborted`로 저장한다.
   어느 경우든 질의 로그(`query_logs`)를 남긴다.

이벤트 이름·데이터를 바꾸면 웹 `src/api/chat.ts`도 함께 고친다.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import anyio
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.conversations import (
    create_conversation,
    get_conversation,
    make_title,
    parse_conversation_id,
)
from app.api.deps import DbSession
from app.api.errors import ApiError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import MessageRole, MessageStatus
from app.db.models import Conversation, Message
from app.providers.base import ChatMessage, EmbeddingProvider, LLMProvider, RerankProvider
from app.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderUnavailable,
    RateLimited,
)
from app.providers.factory import (
    create_embedding_provider,
    create_llm_provider,
    create_rerank_provider,
)
from app.query.graph import QueryDeps, QueryResult, QueryTrace, run_query, save_query_log
from app.query.postprocess import cited_numbers
from app.query.search import SearchFilters

router = APIRouter(tags=["chat"])
log = get_logger(__name__)

KST = ZoneInfo("Asia/Seoul")
HISTORY_TURNS = 6  # 최근 6턴(질문 + 답변 쌍, TRD 7.1 QueryState.history)
STREAMING_STALE = timedelta(minutes=2)
NEW_CONVERSATION = "new"

# 오류 코드 표(TRD 7.3): 코드 → (retryable, 화면 문구)
ERRORS: dict[str, tuple[bool, str]] = {
    "rate_limited": (True, "요청이 많아 잠시 막혔습니다. 잠시 후 다시 시도하세요"),
    "provider_unavailable": (True, "모델 서버에 연결하지 못했습니다"),
    "provider_auth": (False, "API 키 또는 사용량을 확인하세요(관리자 조치 필요)"),
    "timeout": (True, "응답이 너무 오래 걸렸습니다"),
    "conflict": (False, "이전 답변이 끝난 뒤 다시 보내세요"),
    "internal": (True, "문제가 발생했습니다"),
}


def error_code_for(exc: BaseException) -> str:
    if isinstance(exc, RateLimited):
        return "rate_limited"
    if isinstance(exc, ProviderUnavailable):
        return "provider_unavailable"
    if isinstance(exc, ProviderAuthError | ProviderConfigError):
        return "provider_auth"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "internal"


def error_payload(code: str) -> dict[str, Any]:
    retryable, message = ERRORS[code]
    return {"code": code, "message": message, "retryable": retryable}


# ---------------------------------------------------------------------------
# 요청
# ---------------------------------------------------------------------------


class ChatFilters(BaseModel):
    """채팅 필터(TRD 7.2, F-07). 날짜는 한국 시간 그날 시작·끝으로 바꾼다."""

    directory: str | None = None
    exts: list[str] | None = None
    modified_from: date | None = None
    modified_to: date | None = None
    include_deleted: bool = True

    def to_search(self) -> SearchFilters:
        start = (
            datetime.combine(self.modified_from, datetime.min.time(), tzinfo=KST)
            if self.modified_from
            else None
        )
        end = (
            datetime.combine(self.modified_to, datetime.max.time(), tzinfo=KST)
            if self.modified_to
            else None
        )
        return SearchFilters(
            directory=self.directory,
            exts=self.exts,
            mtime_from=start,
            mtime_to=end,
            include_deleted=self.include_deleted,
        )


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    filters: ChatFilters | None = None
    summarize_document_id: int | None = None


@dataclass
class ChatProviders:
    llm: LLMProvider
    embedder: EmbeddingProvider | None
    reranker: RerankProvider | None


@lru_cache
def _default_providers() -> ChatProviders:
    return ChatProviders(
        llm=create_llm_provider(),
        embedder=create_embedding_provider(),
        reranker=create_rerank_provider(),
    )


def get_chat_providers() -> ChatProviders:
    """FastAPI 의존성. 테스트는 `app.dependency_overrides`로 바꿔 끼운다."""
    try:
        return _default_providers()
    except ProviderConfigError as e:
        log.error("chat.provider.config_error", error=str(e))
        payload = error_payload("provider_auth")
        raise ApiError(503, "provider_auth", payload["message"], extra={"retryable": False}) from e


Providers = Annotated[ChatProviders, Depends(get_chat_providers)]


def _event(name: str, data: object) -> dict[str, str]:
    return {"event": name, "data": json.dumps(data, ensure_ascii=False)}


async def _conversation(db: AsyncSession, conversation_id: str, text: str) -> Conversation:
    """`new`면 대화를 만들고(제목은 질문 앞 40자), 아니면 찾아서 잠근다. 제목이 빈 대화(`POST
    /conversations`로 만든 빈 대화)는 첫 질문으로 제목을 채운다."""
    if conversation_id == NEW_CONVERSATION:
        return await create_conversation(db, make_title(text), source="message")
    conv = await get_conversation(db, parse_conversation_id(conversation_id), lock=True)
    if not conv.title:
        conv.title = make_title(text)
    return conv


async def _abort_stale_streaming(db: AsyncSession, conversation_id: int) -> int:
    result = await db.execute(
        update(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.status == MessageStatus.STREAMING,
            Message.created_at < func.now() - STREAMING_STALE,
        )
        .values(status=MessageStatus.ABORTED)
        .returning(Message.id)
    )
    ids = list(result.scalars())
    if ids:
        log.info("chat.message.stale_aborted", conversation_id=conversation_id, count=len(ids))
    return len(ids)


async def _has_streaming(db: AsyncSession, conversation_id: int) -> bool:
    found = await db.execute(
        select(Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.status == MessageStatus.STREAMING,
        )
        .limit(1)
    )
    return found.first() is not None


async def _history(db: AsyncSession, conversation_id: int) -> tuple[list[ChatMessage], list[int]]:
    """최근 6턴의 완료된 메시지와 직전 답변의 출처 문서 ID(요약 대상 "그 문서")."""
    rows = list(
        (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.status == MessageStatus.COMPLETE,
                )
                .order_by(Message.id.desc())
                .limit(HISTORY_TURNS * 2)
            )
        ).scalars()
    )
    rows.reverse()
    history = [
        ChatMessage("user" if m.role == MessageRole.USER else "assistant", m.content)
        for m in rows
        if m.content
    ]
    previous: list[int] = []
    last_answer = next((m for m in reversed(rows) if m.role == MessageRole.ASSISTANT), None)
    if last_answer is not None:
        cited = set(cited_numbers(last_answer.content))
        for c in last_answer.citations or []:
            n = c.get("n")
            doc_id = c.get("document_id")
            if n is None or doc_id is None or (cited and n not in cited):
                continue
            if doc_id not in previous:
                previous.append(int(doc_id))
    return history, previous


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

_END = "__end__"


@dataclass
class ChatRun:
    db: AsyncSession
    providers: ChatProviders
    conversation_id: int
    message_id: int
    body: MessageIn
    history: list[ChatMessage]
    previous_document_ids: list[int]

    def __post_init__(self) -> None:
        self.trace = QueryTrace()
        self.parts: list[str] = []
        self.sources: list[dict[str, Any]] = []
        self.finalized = False

    async def _finish(
        self, status: MessageStatus, content: str, citations: list[dict[str, Any]]
    ) -> None:
        await self.db.execute(
            update(Message)
            .where(Message.id == self.message_id)
            .values(status=status, content=content, citations=citations)
        )
        await self.db.execute(
            update(Conversation)
            .where(Conversation.id == self.conversation_id)
            .values(updated_at=func.now())
        )
        if not self.trace.saved:
            await save_query_log(self.db, self.trace, self.message_id)
        await self.db.commit()
        self.finalized = True

    def _log_fields(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "intent": self.trace.intent,
            "latency_ms": self.trace.latency_ms.get("total", self.trace.elapsed_ms()),
        }

    async def produce(self, queue: asyncio.Queue[tuple[str, Any]]) -> None:
        settings = get_settings()
        result: QueryResult | None = None
        deps = QueryDeps(
            session=self.db,
            llm=self.providers.llm,
            embedder=self.providers.embedder,
            reranker=self.providers.reranker,
            trace=self.trace,
        )
        try:
            async with asyncio.timeout(settings.chat_timeout_seconds):
                async for name, data in run_query(
                    deps,
                    question=self.body.text,
                    history=self.history,
                    previous_document_ids=self.previous_document_ids,
                    filters=self.body.filters.to_search() if self.body.filters else None,
                    summarize_document_id=self.body.summarize_document_id,
                    message_id=self.message_id,
                ):
                    if name == "result":
                        result = data
                        continue
                    if name == "token":
                        self.parts.append(data["text"])
                    elif name == "sources":
                        self.sources = data
                    await queue.put((name, data))
                assert result is not None
                await self._finish(MessageStatus.COMPLETE, result.answer, result.citations)
        except Exception as e:  # noqa: BLE001 - 모든 실패를 오류 이벤트로 바꾼다
            code = error_code_for(e)
            await self._save_failure(MessageStatus.ERROR)
            log.warning(
                "chat.message.failed",
                code=code,
                error=type(e).__name__,
                **self._log_fields(),
            )
            if code == "internal":
                log.exception("chat.message.internal_error", message_id=self.message_id)
            await queue.put(("error", error_payload(code)))
            await queue.put((_END, None))
            return

        done = {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "not_found": result.not_found,
            "coverage": result.coverage,
            "latency_ms": round(self.trace.latency_ms.get("total", self.trace.elapsed_ms())),
        }
        event = "chat.message.not_found" if result.not_found else "chat.message.completed"
        log.info(
            event,
            not_found=result.not_found,
            sources=len(result.citations),
            cited=len(result.cited),
            answer_chars=len(result.answer),
            retried=self.trace.retried,
            grade=self.trace.grade,
            coverage={k: v for k, v in result.coverage.items() if k != "filters_applied"},
            **self._log_fields(),
        )
        await queue.put(("done", done))
        await queue.put((_END, None))

    async def _save_failure(self, status: MessageStatus) -> None:
        """실패·중단 시 그때까지의 답변과 질의 로그를 저장한다. DB 오류로 실패한 경우를 위해
        트랜잭션을 먼저 되돌린다."""
        if self.finalized:
            return
        try:
            await self.db.rollback()
            self.trace.saved = False
            await self._finish(status, "".join(self.parts).strip(), self.sources)
        except Exception as e:  # noqa: BLE001
            log.error(
                "chat.message.save_failed", error=type(e).__name__, message_id=self.message_id
            )

    async def abort(self) -> None:
        await self._save_failure(MessageStatus.ABORTED)
        log.info("chat.message.aborted", tokens=len(self.parts), **self._log_fields())


async def _stream(run: ChatRun) -> AsyncIterator[dict[str, str]]:
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(run.produce(queue))
    ended = False
    try:
        while True:
            name, data = await queue.get()
            if name == _END:
                ended = True
                break
            yield _event(name, data)
    finally:
        if not ended:
            # 클라이언트가 연결을 끊음: 생성을 멈추고 그때까지의 답변을 aborted로 저장한다
            with anyio.CancelScope(shield=True):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await run.abort()


@router.post("/conversations/{conversation_id}/messages")
async def post_message(
    conversation_id: str, body: MessageIn, db: DbSession, providers: Providers
) -> EventSourceResponse:
    conv = await _conversation(db, conversation_id, body.text)
    await _abort_stale_streaming(db, conv.id)
    if await _has_streaming(db, conv.id):
        await db.commit()  # 오래된 streaming 정리는 남긴다
        log.info("chat.message.conflict", conversation_id=conv.id)
        payload = error_payload("conflict")
        raise ApiError(409, "conflict", payload["message"], extra={"retryable": False})

    history, previous = await _history(db, conv.id)
    user_msg = Message(
        conversation_id=conv.id,
        role=MessageRole.USER,
        content=body.text,
        status=MessageStatus.COMPLETE,
    )
    assistant = Message(
        conversation_id=conv.id,
        role=MessageRole.ASSISTANT,
        content="",
        status=MessageStatus.STREAMING,
    )
    db.add_all([user_msg, assistant])
    await db.flush()
    await db.commit()

    # 질문 원문은 기본적으로 앱 로그에 남기지 않고 길이만 남긴다(TRD 11장).
    # 개발·E2E에서 LOG_QUERY_TEXT=true일 때만 원문을 `text`로 함께 기록한다.
    extra: dict[str, object] = {"text": body.text} if get_settings().log_query_text else {}
    log.info(
        "chat.message.received",
        conversation_id=conv.id,
        message_id=assistant.id,
        length=len(body.text),
        history=len(history),
        filters=sorted(body.filters.to_search().applied()) if body.filters else [],
        summarize_document_id=body.summarize_document_id,
        **extra,
    )
    run = ChatRun(
        db=db,
        providers=providers,
        conversation_id=conv.id,
        message_id=assistant.id,
        body=body,
        history=history,
        previous_document_ids=previous,
    )
    return EventSourceResponse(
        _stream(run),
        headers={"X-Accel-Buffering": "no", "X-Conversation-Id": str(conv.id)},
    )
