"""하루 임베딩 한도 (PRD 3장, TRD 6.2).

- 날짜 키는 한국 시간(`TZ`, 기본 Asia/Seoul)의 날짜다. 새 날짜 행이 생기면 사용량이 0부터 시작하므로
  자정에 따로 초기화하지 않는다.
- `used`: 차감한 파일 버전 수(`EMBED_DAILY_FILE_LIMIT`, 기본 100). 버전마다 한 번, 작업을 가져갈 때.
- `chunks_used`: 실제로 API에 보낸 청크 수(`EMBED_DAILY_CHUNK_LIMIT`, 0이면 제한 없음).
- 같은 날짜 행을 `FOR UPDATE`로 잠가, 여러 Worker가 동시에 가져가도 한도를 넘지 않게 한다.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import EmbedQuota

QuotaReason = Literal["files", "chunks"]


def quota_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)


def quota_day(now: datetime) -> date:
    """한도 날짜 키(한국 시간 날짜)."""
    return now.astimezone(quota_tz()).date()


def next_reset(now: datetime) -> datetime:
    """다음 날 0시(한국 시간). 한도에 걸린 embed 작업은 이때까지 쉰다."""
    tz = quota_tz()
    tomorrow = now.astimezone(tz).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(0, 0), tzinfo=tz)


@dataclass(frozen=True)
class QuotaExhausted:
    """한도에 걸려 embed 작업을 가져가지 못함. `resume_at`(다음 KST 자정)까지 쉰다."""

    reason: QuotaReason
    day: date
    used: int
    limit: int
    resume_at: datetime


async def lock_quota(session: AsyncSession, day: date) -> EmbedQuota:
    """날짜 행을 만들고(없으면) 잠근다. 트랜잭션이 끝날 때까지 다른 Worker는 기다린다."""
    await session.execute(insert(EmbedQuota).values(day=day).on_conflict_do_nothing())
    row = await session.scalar(
        select(EmbedQuota)
        .where(EmbedQuota.day == day)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


def chunk_limit_reached(row: EmbedQuota) -> bool:
    limit = get_settings().embed_daily_chunk_limit
    return limit > 0 and row.chunks_used >= limit


def file_limit_reached(row: EmbedQuota) -> bool:
    return row.used >= get_settings().embed_daily_file_limit


def exhausted(row: EmbedQuota, reason: QuotaReason, now: datetime) -> QuotaExhausted:
    settings = get_settings()
    if reason == "chunks":
        used, limit = row.chunks_used, settings.embed_daily_chunk_limit
    else:
        used, limit = row.used, settings.embed_daily_file_limit
    return QuotaExhausted(
        reason=reason, day=row.day, used=used, limit=limit, resume_at=next_reset(now)
    )


async def add_chunks_used(session: AsyncSession, now: datetime, count: int) -> None:
    """API로 보낸 청크 수를 오늘 사용량에 더한다(결과를 버리더라도 API는 불렀으므로 센다)."""
    if count <= 0:
        return
    row = await lock_quota(session, quota_day(now))
    row.chunks_used += count
