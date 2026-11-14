"""로컬 삭제 문서의 보관 기간 경과 영구 삭제 (F-19, TRD 6.5).

Worker의 APScheduler가 매일 04:00(한국 시간)에 부른다. `deleted_at`부터
`LOCAL_DELETED_RETENTION_DAYS`(기본 30일)가 지난 로컬 삭제 문서를
`purge_documents(reason="retention")`로 지운다.
로컬 삭제는 프라이버시 때문에 지운 것이 아니므로 예전 대화의 인용은 그대로 둔다.
로그 `documents.purged`(원인 `retention`, 개수만)는 `purge_documents`가 남긴다.
"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document
from app.ingest.purge import PurgeResult, purge_documents


def retention_cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)


async def expired_document_ids(session: AsyncSession, *, now: datetime, days: int) -> list[int]:
    cutoff = retention_cutoff(now, days)
    rows = await session.scalars(
        select(Document.id)
        .where(
            Document.locally_deleted.is_(True),
            Document.deleted_at.is_not(None),
            Document.deleted_at <= cutoff,
        )
        .order_by(Document.id)
    )
    return list(rows.all())


async def purge_expired(session: AsyncSession, *, now: datetime, days: int) -> PurgeResult:
    """보관 기간이 지난 로컬 삭제 문서를 영구 삭제한다. commit은 호출한 쪽이 한다."""
    ids = await expired_document_ids(session, now=now, days=days)
    return await purge_documents(session, ids, reason="retention")
