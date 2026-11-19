"""`jobs` 테이블 기반 작업 큐 (TRD 6.2).

- 가져가기: `SELECT … FOR UPDATE SKIP LOCKED`로 대기(`queued`)이고 `run_after`가 지난 작업 중
  우선순위(파일 수정 시각) 최신부터 하나. 가져가면 `running` + `locked_at` + `attempts` 1 증가.
  여러 Worker가 동시에 가져가도 한 작업은 한 곳에만 간다.
- 멈춘 작업: `locked_at`이 `JOB_STALE_LOCK_MINUTES`(10분) 넘게 갱신되지 않은 `running` 작업은
  다시 `queued`로 돌린다(Worker 강제 종료 등). 긴 작업은 진행 중에 `locked_at`을 갱신한다.
- 반납: 일시 오류는 `run_after`를 미뤄 다시 `queued`로,
  재시도 상한(`JOB_MAX_ATTEMPTS`)을 넘으면 `failed`.
- 시각은 호출하는 쪽이 넘긴다(테스트가 시계를 바꿀 수 있게). DB의 now()는 쓰지 않는다.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import JobStatus, JobType
from app.db.models import Document, Job

_ERROR_MAX = 1000


async def enqueue(
    session: AsyncSession,
    *,
    type: JobType,
    document: Document,
    run_after: datetime | None = None,
) -> Job:
    """문서의 현재 버전(`sha256`)으로 작업을 등록한다. 우선순위는 파일 수정 시각."""
    job = Job(
        type=type,
        document_id=document.id,
        sha256=document.sha256,
        priority=document.mtime,
        status=JobStatus.QUEUED,
    )
    if run_after is not None:
        job.run_after = run_after
    session.add(job)
    await session.flush()
    return job


def ready_jobs(types: Sequence[JobType], now: datetime) -> Select[Job]:
    """가져갈 수 있는 작업(대기 중, `run_after` 지남) 조회. 잠긴 행은 건너뛴다."""
    return (
        select(Job)
        .where(
            Job.status == JobStatus.QUEUED,
            Job.type.in_(list(types)),
            Job.run_after <= now,
        )
        .order_by(Job.priority.desc(), Job.id)
        .with_for_update(skip_locked=True, of=Job)
    )


async def lock_next(
    session: AsyncSession, types: Sequence[JobType], now: datetime, *, skip: Sequence[int] = ()
) -> Job | None:
    """다음 작업 하나를 잠근다(상태는 아직 바꾸지 않음). 트랜잭션은 호출한 쪽이 끝낸다."""
    stmt = ready_jobs(types, now)
    if skip:
        stmt = stmt.where(Job.id.not_in(list(skip)))
    job: Job | None = await session.scalar(stmt.limit(1).execution_options(populate_existing=True))
    return job


def mark_running(job: Job, now: datetime) -> None:
    job.status = JobStatus.RUNNING
    job.locked_at = now
    job.attempts += 1


def mark_done(job: Job, now: datetime) -> None:
    job.status = JobStatus.DONE
    job.locked_at = None
    job.updated_at = now


def mark_released(job: Job, *, run_after: datetime, error: str) -> None:
    """일시 오류: 다시 대기열로(`run_after` 이후)."""
    job.status = JobStatus.QUEUED
    job.locked_at = None
    job.run_after = run_after
    job.last_error = error[:_ERROR_MAX]


def mark_failed(job: Job, now: datetime, *, error: str) -> None:
    job.status = JobStatus.FAILED
    job.locked_at = None
    job.last_error = error[:_ERROR_MAX]
    job.updated_at = now


async def lock_job(session: AsyncSession, job_id: int) -> Job:
    job = await session.scalar(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None:  # 문서가 영구 삭제되면 작업도 함께 지워진다(CASCADE)
        raise JobGone(job_id)
    return job


class JobGone(Exception):
    """처리 중에 작업 행이 사라짐(문서 영구 삭제)."""


async def touch(session: AsyncSession, job_id: int, now: datetime) -> None:
    """진행 중임을 알린다(`locked_at` 갱신). 멈춘 작업으로 오인되지 않게 한다."""
    await session.execute(
        update(Job).where(Job.id == job_id, Job.status == JobStatus.RUNNING).values(locked_at=now)
    )


async def requeue_stale(
    session: AsyncSession, *, now: datetime, stale_after: timedelta
) -> list[int]:
    """`locked_at`이 오래된 `running` 작업을 다시 대기열에 넣는다. 돌려놓은 작업 ID 목록."""
    result = await session.execute(
        update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.locked_at < now - stale_after)
        .values(status=JobStatus.QUEUED, locked_at=None, run_after=now)
        .returning(Job.id)
    )
    return list(result.scalars())
