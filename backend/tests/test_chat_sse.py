"""채팅 SSE API (TRD 7.3, 8.3, 14장 "SSE 이벤트 순서(일반, 찾지 못함, 요약, 오류, 409)").

`POST /api/conversations/{id}/messages`를 테스트 DB 세션(끝나면 롤백)과 Fake Provider로 부른다.
httpx ASGITransport는 응답을 끝까지 모아서 돌려주므로 순서 검사에 쓰고, 연결 끊김은 ASGI 앱을 직접
불러 `http.disconnect`를 보내는 방식으로 확인한다.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from typing import Any

import httpx
import pytest
from chat_seed import fake_llm, parse_sse, seed_corpus, seed_doc
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.api.chat import ChatProviders
from app.core.config import get_settings
from app.db.enums import MessageRole, MessageStatus
from app.db.models import Conversation, Message, QueryLog
from app.main import app
from app.providers.base import ChatMessage
from app.providers.errors import ProviderAuthError, ProviderUnavailable, RateLimited
from app.query import prompts
from app.query.fake_llm import fake_query_responder
from app.query.graph import NOT_FOUND_TEXT

Events = list[tuple[str, Any]]
CSRF_HEADERS = {"X-Requested-With": "fetch", "Origin": "https://testserver"}


async def ask(
    client: httpx.AsyncClient, text: str, conversation: str | int = "new", **body: Any
) -> tuple[httpx.Response, Events]:
    res = await client.post(
        f"/api/conversations/{conversation}/messages", json={"text": text, **body}
    )
    events = parse_sse(res.text) if res.status_code == 200 else []
    return res, events


def names(events: Events) -> list[str]:
    return [n for n, _ in events]


def assert_order(events: Events, *, status_first: bool = True) -> None:
    """status(여러 번) → sources → token(여러 번) → done."""
    seq = names(events)
    i = seq.index("sources")
    if status_first:
        assert i > 0
    assert set(seq[:i]) <= {"status"}
    assert seq[i + 1 : -1] and set(seq[i + 1 : -1]) == {"token"}
    assert seq[-1] == "done"


async def assistant_message(session: AsyncSession, message_id: int) -> Message:
    msg = (await session.execute(select(Message).where(Message.id == message_id))).scalar_one()
    await session.refresh(msg)
    return msg


async def test_event_order_answer(chat_client: httpx.AsyncClient, session: AsyncSession) -> None:
    docs = await seed_corpus(session)

    with capture_logs() as logs:
        res, events = await ask(chat_client, "알파 프로젝트 마감일 알려줘")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["x-accel-buffering"] == "no"
    assert_order(events)

    stages = [d["stage"] for n, d in events if n == "status"]
    assert stages == ["searching", "reranking", "grading", "generating"]
    sources = next(d for n, d in events if n == "sources")
    assert sources[0]["n"] == 1
    assert sources[0]["document_id"] == docs["project"].id
    assert set(sources[0]) >= {
        "n",
        "chunk_id",
        "document_id",
        "path",
        "location",
        "mtime",
        "snippet",
        "locally_deleted",
    }
    answer = "".join(d["text"] for n, d in events if n == "token")
    assert "12월 15일" in answer
    cited = [int(n) for n in re.findall(r"\[(\d+)\]", answer)]
    assert cited and all(sources[n - 1]["document_id"] == docs["project"].id for n in cited)

    done = events[-1][1]
    assert done["not_found"] is False
    assert done["coverage"]["total_docs"] == 4
    assert isinstance(done["latency_ms"], int)
    assert str(done["conversation_id"]) == res.headers["x-conversation-id"]

    # 저장: 대화(제목=질문 앞 40자), 사용자 메시지, 완료된 답변과 인용
    conv = (
        await session.execute(
            select(Conversation).where(Conversation.id == done["conversation_id"])
        )
    ).scalar_one()
    assert conv.title == "알파 프로젝트 마감일 알려줘"
    msg = await assistant_message(session, done["message_id"])
    assert msg.status == MessageStatus.COMPLETE
    assert msg.content == answer.strip()
    assert msg.citations == sources
    roles = (
        await session.execute(
            select(Message.role).where(Message.conversation_id == conv.id).order_by(Message.id)
        )
    ).scalars()
    assert list(roles) == [MessageRole.USER, MessageRole.ASSISTANT]

    received = [r for r in logs if r["event"] == "chat.message.received"]
    completed = [r for r in logs if r["event"] == "chat.message.completed"]
    assert received and received[0]["message_id"] == done["message_id"]
    assert completed and completed[0]["not_found"] is False
    assert "text" not in received[0]  # 운영 기본값: 질문 원문을 로그에 남기지 않음

    # 같은 대화에 이어서 물으면 이력이 생겨 재작성 단계부터 시작한다
    res, events = await ask(chat_client, "담당자는?", conversation=done["conversation_id"])
    assert res.status_code == 200
    assert [d["stage"] for n, d in events if n == "status"][0] == "rewriting"
    assert events[-1][0] == "done"


async def test_event_order_not_found(chat_client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed_corpus(session)

    with capture_logs() as logs:
        res, events = await ask(chat_client, "화성 기지의 산소 공급량은?")
    assert res.status_code == 200
    assert_order(events)
    stages = [d["stage"] for n, d in events if n == "status"]
    assert "retrying" in stages and "generating" not in stages
    sources = next(d for n, d in events if n == "sources")
    assert len(sources) == 3 and all(s["n"] is None for s in sources)
    tokens = [d for n, d in events if n == "token"]
    assert tokens == [{"text": NOT_FOUND_TEXT}]
    done = events[-1][1]
    assert done["not_found"] is True
    assert done["coverage"] == {
        "bm25_docs": 4,
        "vector_docs": 4,
        "pending_docs": 0,
        "total_docs": 4,
        "filters_applied": {},
    }
    msg = await assistant_message(session, done["message_id"])
    assert msg.status == MessageStatus.COMPLETE and msg.content == NOT_FOUND_TEXT
    assert [r for r in logs if r["event"] == "chat.message.not_found"]

    # 필터 결과 0개: status·sources 없이 token → done(not_found, coverage)
    res, events = await ask(
        chat_client, "위약금 알려줘", filters={"directory": r"C:\없는폴더", "exts": ["md"]}
    )
    assert names(events) == ["token", "done"]
    assert "필터 조건에 맞는 문서가 없습니다" in events[0][1]["text"]
    assert events[1][1]["not_found"] is True
    assert events[1][1]["coverage"]["filters_applied"] == {
        "directory": r"C:\없는폴더",
        "exts": ["md"],
    }


async def test_event_order_summarize(chat_client: httpx.AsyncClient, session: AsyncSession) -> None:
    long_paragraphs = [f"{i}번째 장의 내용. " + "가나다라마바사 " * 700 for i in range(1, 4)]
    report = await seed_doc(session, r"C:\docs\보고서\연간보고서.md", long_paragraphs)
    await seed_doc(session, r"C:\docs\a\공통.md", ["가"])
    await seed_doc(session, r"C:\docs\b\공통.md", ["나"])

    res, events = await ask(chat_client, "연간보고서.md 요약해줘")
    assert res.status_code == 200
    assert_order(events)
    statuses = [d for n, d in events if n == "status"]
    assert all(s["stage"] == "summarizing" for s in statuses)
    progress = [(s["progress"]["done"], s["progress"]["total"]) for s in statuses]
    assert progress == [(0, 3), (1, 3), (2, 3), (3, 3)]
    sources = next(d for n, d in events if n == "sources")
    assert [(s["n"], s["document_id"]) for s in sources] == [(1, report.id)]
    answer = "".join(d["text"] for n, d in events if n == "token")
    assert answer.startswith("문서 요약:") and answer.strip().endswith("[1]")
    assert events[-1][1]["not_found"] is False

    # 요약 대상이 여러 개: sources(후보) → token(고르라는 안내) → done
    res, events = await ask(chat_client, "공통.md 요약해줘")
    assert names(events) == ["sources", "token", "done"]
    assert len(events[0][1]) == 2
    # 고른 문서로 다시 요청(summarize_document_id)하면 요약한다
    chosen = events[0][1][0]["document_id"]
    res, events = await ask(chat_client, "공통.md 요약해줘", summarize_document_id=chosen)
    assert names(events)[0] == "status" and events[-1][0] == "done"
    assert next(d for n, d in events if n == "sources")[0]["document_id"] == chosen


class FailingLLM:
    """`fail_on` 작업에서 오류를 낸다. 생성은 `tokens`개 조각을 보낸 뒤 실패한다."""

    model = "failing-llm"

    def __init__(self, error: Exception, fail_on: str, tokens: int = 0) -> None:
        self.error = error
        self.fail_on = fail_on
        self.tokens = tokens

    async def complete(self, messages: Sequence[ChatMessage], **_: Any) -> str:
        if prompts.task_of(messages) == self.fail_on:
            raise self.error
        return fake_query_responder(messages) or ""

    async def stream(self, messages: Sequence[ChatMessage], **_: Any) -> AsyncIterator[str]:
        if prompts.task_of(messages) == self.fail_on:
            for i in range(self.tokens):
                yield f"조각{i} "
            raise self.error
        yield fake_query_responder(messages) or ""


@pytest.mark.parametrize(
    ("error", "fail_on", "tokens", "code", "retryable"),
    [
        (RateLimited("429"), prompts.TASK_GRADE, 0, "rate_limited", True),
        (ProviderUnavailable("503"), prompts.TASK_GENERATE, 2, "provider_unavailable", True),
        (ProviderAuthError("403"), prompts.TASK_GRADE, 0, "provider_auth", False),
        (RuntimeError("버그"), prompts.TASK_GRADE, 0, "internal", True),
    ],
)
async def test_error_event_has_retryable(
    chat_client: httpx.AsyncClient,
    session: AsyncSession,
    providers: ChatProviders,
    error: Exception,
    fail_on: str,
    tokens: int,
    code: str,
    retryable: bool,
) -> None:
    await seed_corpus(session)
    providers.llm = FailingLLM(error, fail_on, tokens)

    with capture_logs() as logs:
        res, events = await ask(chat_client, "알파 프로젝트 마감일 알려줘")
    assert res.status_code == 200
    assert events[-1][0] == "error"
    assert "done" not in names(events)
    payload = events[-1][1]
    assert payload["code"] == code
    assert payload["retryable"] is retryable
    assert payload["message"]

    # 이미 보낸 토큰은 status=error로 저장한다
    msg_id = (
        await session.execute(
            select(func.max(Message.id)).where(Message.role == MessageRole.ASSISTANT)
        )
    ).scalar_one()
    msg = await assistant_message(session, msg_id)
    assert msg.status == MessageStatus.ERROR
    assert msg.content == "".join(f"조각{i} " for i in range(tokens)).strip()
    failed = [r for r in logs if r["event"] == "chat.message.failed"]
    assert failed and failed[0]["code"] == code
    # 실패해도 질의 로그는 남는다
    qlog = (
        await session.execute(select(QueryLog).where(QueryLog.message_id == msg_id))
    ).scalar_one()
    assert qlog.coverage["total_docs"] == 4


async def test_error_event_timeout(
    chat_client: httpx.AsyncClient,
    session: AsyncSession,
    providers: ChatProviders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_corpus(session)
    monkeypatch.setenv("CHAT_TIMEOUT_SECONDS", "0.3")
    get_settings.cache_clear()
    providers.llm = fake_llm(token_delay=0.2)
    res, events = await ask(chat_client, "알파 프로젝트 마감일 알려줘")
    assert events[-1] == (
        "error",
        {"code": "timeout", "message": "응답이 너무 오래 걸렸습니다", "retryable": True},
    )


async def test_conflict_409_while_streaming(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed_corpus(session)
    conv = Conversation(title="진행 중")
    session.add(conv)
    await session.flush()
    streaming = Message(
        conversation_id=conv.id, role=MessageRole.ASSISTANT, status=MessageStatus.STREAMING
    )
    session.add(streaming)
    await session.commit()

    res, _ = await ask(chat_client, "알파 프로젝트 마감일 알려줘", conversation=conv.id)
    assert res.status_code == 409
    assert res.json() == {
        "error": {
            "code": "conflict",
            "message": "이전 답변이 끝난 뒤 다시 보내세요",
            "retryable": False,
        }
    }
    count = (
        await session.execute(select(func.count()).where(Message.conversation_id == conv.id))
    ).scalar_one()
    assert count == 1  # 새 메시지를 저장하지 않았다

    # 2분 넘게 streaming인 메시지는 aborted로 정리하고 새 질문을 받는다
    await session.execute(
        Message.__table__.update()
        .where(Message.id == streaming.id)
        .values(created_at=func.now() - timedelta(minutes=3))
    )
    await session.commit()
    res, events = await ask(chat_client, "알파 프로젝트 마감일 알려줘", conversation=conv.id)
    assert res.status_code == 200 and events[-1][0] == "done"
    old = await assistant_message(session, streaming.id)
    assert old.status == MessageStatus.ABORTED

    # 없는 대화
    res, _ = await ask(chat_client, "질문", conversation=999999999)
    assert res.status_code == 404


async def test_disconnect_saves_aborted(
    chat_client: httpx.AsyncClient, session: AsyncSession, providers: ChatProviders
) -> None:
    await seed_corpus(session)
    providers.llm = fake_llm(
        token_delay=0.05,
        rules=[],
        responder=lambda m: (
            "알파 프로젝트 마감일은 12월 15일이다 [1]. " * 20
            if prompts.task_of(m) == prompts.TASK_GENERATE
            else fake_query_responder(m)
        ),
    )

    # ASGI 앱을 직접 불러, 토큰 2개를 받은 뒤 연결을 끊는다(http.disconnect)
    body = json.dumps({"text": "알파 프로젝트 마감일 알려줘"}).encode()
    disconnect = asyncio.Event()
    received = b""
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal received
        if message["type"] == "http.response.body":
            received += message.get("body", b"")
            if received.count(b"event: token") >= 2:
                disconnect.set()

    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        *((k.lower().encode(), v.encode()) for k, v in CSRF_HEADERS.items()),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/conversations/new/messages",
        "raw_path": b"/api/conversations/new/messages",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 443),
    }
    with capture_logs() as logs:
        await asyncio.wait_for(app(scope, receive, send), timeout=10)

    events = parse_sse(received.decode())
    assert "done" not in names(events) and "error" not in names(events)
    aborted = [r for r in logs if r["event"] == "chat.message.aborted"]
    assert aborted
    msg = await assistant_message(session, aborted[0]["message_id"])
    assert msg.status == MessageStatus.ABORTED
    # 끊기 전까지 보낸 답변이 남고, 생성은 중단되어 전체 답변보다 짧다
    assert msg.content.startswith("알파 프로젝트")
    assert len(msg.content) < len("알파 프로젝트 마감일은 12월 15일이다 [1]. " * 20) // 2
    assert msg.citations and msg.citations[0]["n"] == 1
    qlog = (
        await session.execute(select(QueryLog).where(QueryLog.message_id == msg.id))
    ).scalar_one()
    assert qlog.coverage["total_docs"] == 4


async def test_query_log_saved_with_coverage(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed_corpus(session)
    res, events = await ask(chat_client, "알파 프로젝트 마감일 알려줘", filters={"exts": ["md"]})
    done = events[-1][1]
    qlog = (
        await session.execute(select(QueryLog).where(QueryLog.message_id == done["message_id"]))
    ).scalar_one()
    assert qlog.rewritten_query == "알파 프로젝트 마감일 알려줘"
    assert qlog.not_found is False and qlog.retried is False
    assert qlog.grade == "sufficient"
    assert qlog.filters["exts"] == ["md"]
    assert qlog.coverage == {
        "bm25_docs": 3,
        "vector_docs": 3,
        "pending_docs": 0,
        "total_docs": 3,
        "filters_applied": {"exts": ["md"]},
    }
    assert qlog.coverage == done["coverage"]
    assert {
        "scope",
        "retrieve",
        "embed",
        "sql",
        "rerank",
        "grade",
        "generate",
        "first_token",
        "total",
    } <= set(qlog.latency_ms)
    assert qlog.models == {
        "llm": "fake-llm",
        "embed": "fake-embed",
        "rerank": "fake-rerank",
        "intent": "qa",
    }
    assert qlog.retrieved and all(
        {"chunk_id", "rank", "rrf", "rerank", "selected", "attempt"} <= set(e)
        for e in qlog.retrieved
    )
    assert sum(1 for e in qlog.retrieved if e["selected"]) == len(
        next(d for n, d in events if n == "sources")
    )

    # 찾지 못함도 coverage와 함께 남는다
    res, events = await ask(chat_client, "화성 기지의 산소 공급량은?")
    qlog = (
        await session.execute(
            select(QueryLog).where(QueryLog.message_id == events[-1][1]["message_id"])
        )
    ).scalar_one()
    assert qlog.not_found is True and qlog.retried is True and qlog.grade == "insufficient"
    assert qlog.coverage["total_docs"] == 4


async def test_fake_error_marker_fails_once_then_answers(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """E2E 오류 재현 표식: 같은 질문의 첫 요청만 실패하고(다시 시도 가능) 다시 보내면 답한다."""
    await seed_corpus(session)
    question = "알파 프로젝트 마감일 [[fake-error-once:provider_unavailable]] pytest-once"
    res, events = await ask(chat_client, question)
    assert names(events)[-1] == "error"
    assert events[-1][1] == {
        "code": "provider_unavailable",
        "message": "모델 서버에 연결하지 못했습니다",
        "retryable": True,
    }
    conv = int(res.headers["X-Conversation-Id"])
    _, again = await ask(chat_client, question, conv)
    assert names(again)[-1] == "done"
    assert again[-1][1]["not_found"] is False
