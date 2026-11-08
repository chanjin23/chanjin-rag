"""동기화 실행 기록 `sync_runs` (TRD 5.1, 6.1, 8.2).

- 시작: `running`, `heartbeat_at`=시작 시각.
- 하트비트: Agent가 실행 중 1분마다 부른다. 하트비트가 끊겨 `abandoned`가 된 실행에 하트비트가 다시
  오면(PC가 절전에서 깨어남) `running`으로 되돌린다.
- 완료: `succeeded` 또는 `failed`와 Agent 통계를 저장한다. 서버가 판정 때 기록한 통계 키
  (`manifest`, `deletion_held`)는 Agent 값으로 덮지 않는다.
- `abandon_stale_runs`: `heartbeat_at`이 `SYNC_RUN_STALE_MINUTES`(10분) 넘게 갱신되지 않은
  `running` 실행을 `abandoned`로 바꾼다. Worker의 APScheduler가 1분마다 부른다.
- 시각은 호출하는 쪽이 넘긴다(테스트가 시계를 바꿀 수 있게).
"""

from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import SyncRunSource, SyncRunStatus
from app.db.models import SyncRun

log = get_logger(__name__)

# 서버가 채우는 통계 키. Agent가 완료 때 보낸 통계가 이 키를 덮지 않는다.
SERVER_STATS_KEYS = frozenset({"manifest", "deletion_held"})

FinalStatus = Literal["succeeded", "failed"]


class RunNotFound(Exception):
    pass


class RunFinished(Exception):
    """이미 끝난(`succeeded`·`failed`) 실행에 대한 요청."""

    def __init__(self, status: SyncRunStatus) -> None:
        super().__init__(status.value)
        self.status = status


async def start_run(session: AsyncSession, source: SyncRunSource, now: datetime) -> SyncRun:
    run = SyncRun(source=source, started_at=now, heartbeat_at=now, status=SyncRunStatus.RUNNING)
    session.add(run)
    await session.flush()
    log.info("sync.run.started", run_id=run.id, source=source.value)
    return run


async def get_run(session: AsyncSession, run_id: int, *, for_update: bool = True) -> SyncRun:
    stmt = select(SyncRun).where(SyncRun.id == run_id)
    if for_update:
        stmt = stmt.with_for_update()
    run = await session.scalar(stmt)
    if run is None:
        raise RunNotFound(str(run_id))
    return run


def _ensure_active(run: SyncRun) -> None:
    if run.status in (SyncRunStatus.SUCCEEDED, SyncRunStatus.FAILED):
        raise RunFinished(run.status)


async def get_active_run(session: AsyncSession, run_id: int) -> SyncRun:
    """진행 중(`running`·`abandoned`)인 실행. 끝난 실행이면 `RunFinished`."""
    run = await get_run(session, run_id)
    _ensure_active(run)
    return run


async def heartbeat(session: AsyncSession, run_id: int, now: datetime) -> SyncRun:
    run = await get_active_run(session, run_id)
    run.heartbeat_at = now
    if run.status == SyncRunStatus.ABANDONED:
        run.status = SyncRunStatus.RUNNING
        run.finished_at = None
        log.info("sync.run.resumed", run_id=run.id)
    await session.flush()
    return run


async def complete_run(
    session: AsyncSession,
    run_id: int,
    *,
    status: FinalStatus,
    stats: dict[str, Any],
    error: str | None,
    now: datetime,
) -> SyncRun:
    run = await get_active_run(session, run_id)
    merged = {k: v for k, v in stats.items() if k not in SERVER_STATS_KEYS}
    merged.update({k: v for k, v in run.stats.items() if k in SERVER_STATS_KEYS})
    if error:
        merged["error"] = error[:1000]
    run.stats = merged
    run.status = SyncRunStatus(status)
    run.finished_at = now
    run.heartbeat_at = now
    await session.flush()
    log.info(
        "sync.run.completed",
        run_id=run.id,
        status=run.status.value,
        duration_s=round((now - run.started_at).total_seconds(), 1),
        stats_keys=sorted(merged),
    )
    return run


async def abandon_stale_runs(
    session: AsyncSession, *, now: datetime, stale_after: timedelta
) -> list[int]:
    """하트비트가 `stale_after`보다 오래 끊긴 `running` 실행을 `abandoned`로 바꾼다(ID 목록)."""
    rows = await session.execute(
        update(SyncRun)
        .where(SyncRun.status == SyncRunStatus.RUNNING, SyncRun.heartbeat_at < now - stale_after)
        .values(status=SyncRunStatus.ABANDONED, finished_at=now)
        .returning(SyncRun.id)
    )
    ids = sorted(rows.scalars().all())
    if ids:
        log.warning("sync.run.abandoned", run_ids=ids, count=len(ids))
    return ids
