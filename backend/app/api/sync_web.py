"""동기화 화면 API (F-02, F-03, F-05, TRD 6.1, 8.3, 9.3, 12.4). 웹 세션 + CSRF.

Agent 전용 Sync API(`app/api/sync.py`, Bearer 토큰)와 같은 `/sync` 접두사지만 경로가 겹치지 않는다.

- `GET /sync/status`:
  - `running`: 진행 중(`running`)인 최근 실행(없으면 null). 웹은 이때 "지금 동기화" 대신
    진행 상태를 보인다.
  - `latest_run`: 가장 최근 실행, `last_success_at`: 마지막 성공 실행의 종료 시각
  - `queue`: 대기·실행 중 작업 수(종류별 `by_type`)
  - `embed`: 오늘(한국 시간) 임베딩 파일 사용량 `used`/`limit`
  - `last_backup_at`: 최근 백업 시각(백업 생성은 T14. 기록이 없으면 null)
  - `held_deletions`: 판정을 받은 가장 최근 실행이 대량 삭제 보호로 보류했으면 건수·분모·비율·예시와
    승인 시각(`approved_at`, 승인했으면 다음 실행에서 적용)
  - `skipped_formats`: 마지막 성공 실행의 건너뛴 형식별 건수(Agent `stats.skipped_formats`)
  - `stale`: 마지막 성공 동기화·백업이 하루(24시간) 넘었거나 없으면 true(경고 배지). 서버 시각 기준.
- `POST /sync/held-deletions/apply`: 보류된 판정의 적용을 승인한다(다음 manifest 판정에서 적용).
  선택 값 `run_id`가 지금 보류 중인 실행과 다르면 409 `held_deletions_outdated`, 보류가 없으면
  409 `no_held_deletions`.
- `POST /sync/run-requests`: 웹에서 "지금 동기화"를 눌렀다는 기록(202). 실행은 브라우저가
  `chanjin-sync://run?source=web`으로 Agent를 깨워서 하고, 서버는 로그만 남긴다(운영 추적용).
- 로그: `sync.run.requested`,
  `sync.status.read`(debug, 2초 간격 조회라 기본 수준에서는 남기지 않음),
  `sync.held_deletions.applied`.
"""

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DbSession
from app.api.errors import ApiError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import JobStatus, JobType, SyncRunStatus
from app.db.models import EmbedQuota, Job, SyncConfig, SyncRun
from app.jobs.quota import quota_day
from app.jobs.runner import utc_now

router = APIRouter(prefix="/sync", tags=["sync-web"])
log = get_logger(__name__)

STALE_AFTER = timedelta(hours=24)
HELD_SAMPLE = 10


class RunSummary(BaseModel):
    id: int
    source: str
    status: str
    started_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None
    counts: dict[str, int]
    error: str | None


class QueueOut(BaseModel):
    queued: int
    running: int
    by_type: dict[str, int]


class EmbedUsage(BaseModel):
    used: int
    limit: int


class HeldDeletions(BaseModel):
    run_id: int
    count: int
    total: int
    ratio: float
    threshold: float
    sample: list[str]
    approved_at: datetime | None


class StaleOut(BaseModel):
    sync: bool
    backup: bool


class SyncStatusOut(BaseModel):
    now: datetime
    running: RunSummary | None
    latest_run: RunSummary | None
    last_success_at: datetime | None
    queue: QueueOut
    embed: EmbedUsage
    last_backup_at: datetime | None
    held_deletions: HeldDeletions | None
    skipped_formats: dict[str, int]
    stale: StaleOut


_COUNT_KEYS = ("upload", "meta_only", "deleted", "restored", "moved", "copied", "unchanged")


def _summary(run: SyncRun) -> RunSummary:
    manifest = run.stats.get("manifest") if isinstance(run.stats, dict) else None
    counts: dict[str, int] = {}
    if isinstance(manifest, dict):
        counts = {k: int(manifest[k]) for k in ("files", *_COUNT_KEYS) if k in manifest}
    for key in ("uploaded", "upload_failed", "scan_errors"):
        value = run.stats.get(key)
        if isinstance(value, int):
            counts[key] = value
        elif isinstance(value, list):
            counts[key] = len(value)
    error = run.stats.get("error")
    return RunSummary(
        id=run.id,
        source=run.source.value,
        status=run.status.value,
        started_at=run.started_at,
        heartbeat_at=run.heartbeat_at,
        finished_at=run.finished_at,
        counts=counts,
        error=str(error) if error else None,
    )


async def _latest(db: DbSession, *where: Any) -> SyncRun | None:
    stmt = select(SyncRun).where(*where).order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
    return await db.scalar(stmt.limit(1))


async def _held(db: DbSession, config: SyncConfig | None) -> HeldDeletions | None:
    judged = await _latest(db, SyncRun.stats.has_key("manifest"))
    if judged is None:
        return None
    held = judged.stats.get("deletion_held") or {}
    guard = held.get("mass_delete") if isinstance(held, dict) else None
    if not isinstance(guard, dict):
        return None
    approved = (
        config.held_deletions_approved_at
        if config is not None and config.held_deletions_run_id == judged.id
        else None
    )
    return HeldDeletions(
        run_id=judged.id,
        count=int(guard.get("count", 0)),
        total=int(guard.get("total", 0)),
        ratio=float(guard.get("ratio", 0.0)),
        threshold=float(guard.get("threshold", 0.0)),
        sample=[str(p) for p in guard.get("sample", [])][:HELD_SAMPLE],
        approved_at=approved,
    )


def _skipped_formats(run: SyncRun | None) -> dict[str, int]:
    raw = run.stats.get("skipped_formats") if run is not None else None
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, int)}


def _stale(at: datetime | None, now: datetime) -> bool:
    return at is None or now - at > STALE_AFTER


@router.get("/status")
async def sync_status(db: DbSession) -> SyncStatusOut:
    settings = get_settings()
    now = utc_now()
    running = await _latest(db, SyncRun.status == SyncRunStatus.RUNNING)
    latest = await _latest(db)
    success = await _latest(db, SyncRun.status == SyncRunStatus.SUCCEEDED)
    last_success_at = success.finished_at if success is not None else None

    rows = (
        await db.execute(
            select(Job.type, Job.status, func.count())
            .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .group_by(Job.type, Job.status)
        )
    ).all()
    by_type = {t.value: 0 for t in JobType}
    queued = running_jobs = 0
    for job_type, status, count in rows:
        by_type[JobType(job_type).value] += int(count)
        if JobStatus(status) == JobStatus.QUEUED:
            queued += int(count)
        else:
            running_jobs += int(count)

    used = await db.scalar(select(EmbedQuota.used).where(EmbedQuota.day == quota_day(now))) or 0
    config = await db.get(SyncConfig, 1)
    last_backup_at: datetime | None = None  # 백업 생성·기록은 T14
    result = SyncStatusOut(
        now=now,
        running=_summary(running) if running is not None else None,
        latest_run=_summary(latest) if latest is not None else None,
        last_success_at=last_success_at,
        queue=QueueOut(queued=queued, running=running_jobs, by_type=by_type),
        embed=EmbedUsage(used=int(used), limit=settings.embed_daily_file_limit),
        last_backup_at=last_backup_at,
        held_deletions=await _held(db, config),
        skipped_formats=_skipped_formats(success),
        stale=StaleOut(sync=_stale(last_success_at, now), backup=_stale(last_backup_at, now)),
    )
    log.debug(
        "sync.status.read",
        running=result.running is not None,
        held=result.held_deletions is not None,
        queued=queued,
    )
    return result


class ApplyHeldIn(BaseModel):
    run_id: int | None = None


class ApplyHeldOut(BaseModel):
    run_id: int
    count: int
    approved_at: datetime


@router.post("/held-deletions/apply")
async def apply_held_deletions(db: DbSession, body: ApplyHeldIn | None = None) -> ApplyHeldOut:
    config = await db.get(SyncConfig, 1, with_for_update=True)
    held = await _held(db, config)
    if held is None or config is None:
        raise ApiError(409, "no_held_deletions", "보류된 삭제 판정이 없습니다")
    if body is not None and body.run_id is not None and body.run_id != held.run_id:
        raise ApiError(
            409,
            "held_deletions_outdated",
            "보류된 삭제 판정이 바뀌었습니다. 화면을 새로 고쳐 다시 확인해 주세요",
            extra={"run_id": held.run_id},
        )
    now = utc_now()
    config.held_deletions_approved_at = now
    config.held_deletions_run_id = held.run_id
    await db.commit()
    log.info("sync.held_deletions.applied", run_id=held.run_id, count=held.count)
    return ApplyHeldOut(run_id=held.run_id, count=held.count, approved_at=now)


class RunRequestOut(BaseModel):
    requested_at: datetime
    running: bool


@router.post("/run-requests", status_code=202)
async def request_run(db: DbSession) -> RunRequestOut:
    """웹의 "지금 동기화" 클릭 기록. 실행 중인 실행이 있으면 `running=true`(Agent가 바로 끝남)."""
    running = await _latest(db, SyncRun.status == SyncRunStatus.RUNNING)
    now = utc_now()
    log.info(
        "sync.run.requested",
        source="web",
        running_run_id=running.id if running is not None else None,
    )
    return RunRequestOut(requested_at=now, running=running is not None)
