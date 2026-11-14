"""로컬 삭제 문서의 30일 뒤 영구 삭제 (F-19, TRD 6.5).

Worker의 매일 04:00 예약 작업(`retention_purge`)을 테스트 세션과 같은 연결 위에서 실행한다.
문서는 합성 데이터(`chat_seed.seed_doc`)이고 시계는 고정한다.
"""

from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.triggers.cron import CronTrigger
from chat_seed import seed_doc
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.db.enums import MessageRole, MessageStatus
from app.db.models import EMBED_DIM, Chunk, Conversation, Document, Message, Section
from app.jobs.runner import JobRunner
from app.jobs.worker import Worker
from app.providers.fake import FakeEmbeddingProvider

NOW = datetime(2026, 11, 14, 19, 0, tzinfo=UTC)  # 한국 시간 11-15 04:00


def make_worker(session: AsyncSession) -> Worker:
    sm = async_sessionmaker(
        bind=session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    return Worker(JobRunner(sm, FakeEmbeddingProvider(EMBED_DIM), clock=lambda: NOW), sm)


async def seed_deleted(session: AsyncSession, path: str, deleted_days_ago: float | None) -> int:
    doc = await seed_doc(session, path, [f"{path} 본문"])
    if deleted_days_ago is not None:
        doc.locally_deleted = True
        doc.deleted_at = NOW - timedelta(days=deleted_days_ago)
    await session.commit()
    return doc.id


async def exists(session: AsyncSession, doc_id: int) -> bool:
    stmt = select(func.count()).select_from(Document).where(Document.id == doc_id)
    return bool(await session.scalar(stmt))


async def test_purges_after_retention_days(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_DELETED_RETENTION_DAYS", "30")
    get_settings.cache_clear()
    worker = make_worker(session)

    # 매일 04:00(한국 시간) 예약
    job = {j.id: j for j in worker.build_scheduler().get_jobs()}["retention_purge"]
    assert isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("4", "0")
    assert str(job.trigger.timezone) == "Asia/Seoul"

    expired = await seed_deleted(session, r"C:\docs\지난\오래전.md", 31)
    boundary = await seed_deleted(session, r"C:\docs\지난\딱30일.md", 30)
    recent = await seed_deleted(session, r"C:\docs\지난\어제.md", 1)
    alive = await seed_deleted(session, r"C:\docs\지금\살아있음.md", None)
    # 표시만 있고 시각이 없는 문서는 기간을 셀 수 없으니 둔다
    no_time = await seed_deleted(session, r"C:\docs\지난\시각없음.md", 100)
    await session.execute(
        Document.__table__.update().where(Document.id == no_time).values(deleted_at=None)
    )
    await session.commit()

    with capture_logs() as logs:
        purged = await worker.purge_retention()
    assert purged == 2
    assert not await exists(session, expired)
    assert not await exists(session, boundary)
    for kept in (recent, alive, no_time):
        assert await exists(session, kept)
    for gone in (expired, boundary):
        for model in (Section, Chunk):
            n = await session.scalar(
                select(func.count()).select_from(model).where(model.document_id == gone)
            )
            assert n == 0

    events = [e for e in logs if e["event"] == "documents.purged"]
    assert len(events) == 1
    assert events[0]["reason"] == "retention"
    assert events[0]["documents"] == 2
    assert events[0]["masked_messages"] == 0
    assert "C:\\docs" not in str(events)  # 경로는 남기지 않는다

    # 보관 기간 설정을 따른다: 1일로 줄이면 어제 지운 문서도 지운다
    monkeypatch.setenv("LOCAL_DELETED_RETENTION_DAYS", "1")
    get_settings.cache_clear()
    assert await make_worker(session).purge_retention() == 1
    assert not await exists(session, recent)
    assert await exists(session, alive)


async def test_retention_purge_keeps_citations(session: AsyncSession) -> None:
    worker = make_worker(session)
    doc_id = await seed_deleted(session, r"C:\docs\지난\인용됨.md", 45)
    other_id = await seed_deleted(session, r"C:\docs\지금\다른문서.md", None)
    conv = Conversation(title="예전 대화")
    session.add(conv)
    await session.flush()
    citations = [
        {"n": 1, "document_id": doc_id, "path": r"C:\docs\지난\인용됨.md", "snippet": "원래 구절"},
        {"n": 2, "document_id": other_id, "path": r"C:\docs\지금\다른문서.md", "snippet": "다른"},
    ]
    msg = Message(
        conversation_id=conv.id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.COMPLETE,
        content="답변 [1][2]",
        citations=citations,
    )
    session.add(msg)
    await session.commit()

    with capture_logs() as logs:
        assert await worker.purge_retention() == 1
    assert not await exists(session, doc_id)

    await session.refresh(msg)
    # 30일 경과 삭제는 프라이버시 삭제가 아니므로 인용 구절을 가리지 않는다
    assert msg.citations == citations
    assert not any(c.get("deleted") for c in msg.citations)
    purged = next(e for e in logs if e["event"] == "documents.purged")
    assert purged["reason"] == "retention"
    assert purged["masked_messages"] == 0
