"""상태 점검 (TRD 8.3, 12.4).

- `GET /api/health`(공개, 인증 없음): Compose·E2E 준비 확인용. 프로세스가 살아 있으면 항상 200과
  `status: "ok"`, 구성 요소 이상은 `degraded`와 `issues[].code`만 준다(모델 이름·디스크 크기 같은
  세부는 주지 않음).
- `GET /api/health/details`(웹 세션): 같은 점검의 세부. 웹의 전역 배너가 쓴다.
  - `checks.db`: `SELECT 1` 성공 여부
  - `checks.worker`: `worker_heartbeats`의 가장 최근 하트비트. `WORKER_STALE_SECONDS`(90초) 넘게
    없으면 `stale`, 행이 없으면 `missing`
  - `checks.disk`: `DISK_CHECK_PATH`(기본 업로드 임시 폴더)가 있는 디스크의 여유. `DISK_MIN_FREE_GB`
    (10GB) 미만이면 `low`, 확인하지 못하면 `unknown`
  - `checks.providers`: `provider_status`의 종류별 마지막 성공·실패 시각, 오류 코드, 상태 코드,
    `failing`(마지막 실패가 마지막 성공보다 나중), `failing_since`(연속 실패의 첫 시각)
  - `issues`: 배너용 `{code, message, since, level}`. 코드는 `db_unavailable`, `worker_stale`,
    `worker_missing`, `disk_low`, `provider_auth`(키 오류, 402 크레딧 소진 포함).
    `since`는 "같은 이상"을 구분하는 시각이다. `worker_stale`은 마지막 하트비트, `provider_auth`는
    연속 실패의 첫 시각(실패가 이어져도 바뀌지 않음), 그 밖은 이 프로세스가 처음 발견한 시각.
    `level`은 이상이 심해지면 바뀌는 값(`disk_low`: 남은 GB를 내림한 정수). 웹은
    `code`·`since`·`level`로 닫은 배너를 구분해, 바뀌면 다시 보여준다.

로그: 이상이 있으면 `health.degraded`(이상 코드). 웹이 주기적으로 부르므로 이상의 내용(코드·since·
level)이 바뀔 때만 남기고, 정상으로 돌아오면 `health.recovered`를 남긴다. 발견 시각과 마지막 기록은
프로세스 안(`IssueTracker`)에 둔다(API는 프로세스 하나로 띄운다).
"""

import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, text

from app.api.deps import DbSession
from app.core.auth import utcnow
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import ProviderStatus, WorkerHeartbeat

router = APIRouter(tags=["health"])  # 공개
details_router = APIRouter(tags=["health"])  # 웹 세션(app.api.router의 web_router 아래)
log = get_logger(__name__)

GB = 1024**3

IssueCode = Literal["db_unavailable", "worker_stale", "worker_missing", "disk_low", "provider_auth"]


class DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def disk_usage(path: Path) -> DiskUsage:
    """`path`(없으면 가장 가까운 있는 상위 폴더)가 있는 디스크의 사용량. 테스트가 바꿔 끼운다."""
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    total, used, free = shutil.disk_usage(target)
    return DiskUsage(total, used, free)


class DbCheck(BaseModel):
    ok: bool
    error: str | None = None


class WorkerCheck(BaseModel):
    status: Literal["ok", "stale", "missing", "unknown"]
    name: str | None = None
    heartbeat_at: datetime | None = None
    age_seconds: float | None = None
    stale_after_seconds: int


class DiskCheck(BaseModel):
    status: Literal["ok", "low", "unknown"]
    free_bytes: int | None = None
    total_bytes: int | None = None
    min_free_bytes: int


class ProviderCheck(BaseModel):
    kind: str
    model: str | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error_code: str | None
    last_status_code: int | None
    failing: bool
    failing_since: datetime | None


class HealthChecks(BaseModel):
    db: DbCheck
    worker: WorkerCheck
    disk: DiskCheck
    providers: list[ProviderCheck]


class HealthIssue(BaseModel):
    code: IssueCode
    message: str
    since: datetime | None = None
    level: int | None = None


class HealthDetails(BaseModel):
    status: Literal["ok"] = "ok"
    degraded: bool
    now: datetime
    issues: list[HealthIssue]
    checks: HealthChecks


class PublicIssue(BaseModel):
    code: IssueCode


class PublicHealth(BaseModel):
    status: Literal["ok"] = "ok"
    degraded: bool
    issues: list[PublicIssue]


def _failing(row: ProviderStatus) -> bool:
    if row.last_failure_at is None:
        return False
    return row.last_success_at is None or row.last_failure_at > row.last_success_at


async def _check_db(db: DbSession) -> DbCheck:
    try:
        await db.execute(text("SELECT 1"))
    except Exception as e:
        try:
            await db.rollback()
        except Exception:
            pass
        return DbCheck(ok=False, error=type(e).__name__)
    return DbCheck(ok=True)


async def _check_worker(db: DbSession, now: datetime, stale_after: int) -> WorkerCheck:
    row = await db.scalar(
        select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc()).limit(1)
    )
    if row is None:
        return WorkerCheck(status="missing", stale_after_seconds=stale_after)
    age = (now - row.heartbeat_at).total_seconds()
    return WorkerCheck(
        status="stale" if age > stale_after else "ok",
        name=row.name,
        heartbeat_at=row.heartbeat_at,
        age_seconds=round(age, 1),
        stale_after_seconds=stale_after,
    )


def _check_disk(path: Path, min_free: int) -> DiskCheck:
    try:
        usage = disk_usage(path)
    except OSError:
        return DiskCheck(status="unknown", min_free_bytes=min_free)
    return DiskCheck(
        status="low" if usage.free < min_free else "ok",
        free_bytes=usage.free,
        total_bytes=usage.total,
        min_free_bytes=min_free,
    )


def _provider_auth_issue(providers: list[ProviderCheck]) -> HealthIssue | None:
    failing = [p for p in providers if p.failing and p.last_error_code == "provider_auth"]
    if not failing:
        return None
    since = min(p.failing_since or p.last_failure_at for p in failing if p.last_failure_at)
    kinds = ", ".join(p.kind for p in failing)
    if any(p.last_status_code == 402 for p in failing):
        message = f"NVIDIA API 크레딧·사용량이 소진됐습니다({kinds}). 사용량을 확인하세요"
    else:
        message = f"NVIDIA API 인증 오류({kinds}). API 키 또는 사용량을 확인하세요"
    return HealthIssue(code="provider_auth", message=message, since=since)


def build_issues(checks: HealthChecks) -> list[HealthIssue]:
    """점검 결과 → 배너용 이상 목록. `since`가 없는 이상은 `IssueTracker`가 발견 시각을 채운다."""
    issues: list[HealthIssue] = []
    if not checks.db.ok:
        issues.append(HealthIssue(code="db_unavailable", message="DB에 연결하지 못했습니다"))
    w = checks.worker
    if w.status == "stale":
        issues.append(
            HealthIssue(
                code="worker_stale",
                message="Worker 하트비트가 끊겼습니다. 문서 처리가 멈춰 있습니다",
                since=w.heartbeat_at,
            )
        )
    elif w.status == "missing":
        issues.append(
            HealthIssue(
                code="worker_missing",
                message="Worker가 실행된 기록이 없습니다. `rag-backend worker`를 실행하세요",
            )
        )
    d = checks.disk
    if d.status == "low" and d.free_bytes is not None:
        issues.append(
            HealthIssue(
                code="disk_low",
                message=(
                    f"디스크 여유가 {d.free_bytes / GB:.1f}GB 남았습니다"
                    f"({d.min_free_bytes / GB:.0f}GB 미만)"
                ),
                level=int(d.free_bytes // GB),
            )
        )
    auth = _provider_auth_issue(checks.providers)
    if auth is not None:
        issues.append(auth)
    return issues


Signature = tuple[tuple[str, str | None, int | None], ...]


class IssueTracker:
    """이상을 처음 발견한 시각과 마지막으로 기록한 이상(로그 중복 방지)을 기억한다."""

    def __init__(self) -> None:
        self.first_seen: dict[str, datetime] = {}
        self.last_signature: Signature = ()

    def annotate(self, issues: list[HealthIssue], now: datetime) -> None:
        """`since`가 없는 이상에 처음 발견한 시각을 채운다. 사라진 이상의 기록은 지운다."""
        current = {i.code for i in issues}
        for code in list(self.first_seen):
            if code not in current:
                del self.first_seen[code]
        for issue in issues:
            if issue.since is None:
                issue.since = self.first_seen.setdefault(issue.code, now)

    def log_change(self, issues: list[HealthIssue], checks: HealthChecks) -> None:
        signature: Signature = tuple(
            (i.code, i.since.isoformat() if i.since else None, i.level) for i in issues
        )
        if signature == self.last_signature:
            return
        previous = self.last_signature
        self.last_signature = signature
        if issues:
            log.warning(
                "health.degraded",
                issues=[i.code for i in issues],
                worker=checks.worker.status,
                worker_age_s=checks.worker.age_seconds,
                disk=checks.disk.status,
                disk_free_gb=(
                    round(checks.disk.free_bytes / GB, 1)
                    if checks.disk.free_bytes is not None
                    else None
                ),
                providers={p.kind: p.last_error_code for p in checks.providers if p.failing},
            )
        elif previous:
            log.info("health.recovered", previous=[code for code, _, _ in previous])


tracker = IssueTracker()


async def collect_health(db: DbSession) -> HealthDetails:
    settings = get_settings()
    now = utcnow()
    db_check = await _check_db(db)
    providers: list[ProviderCheck] = []
    if db_check.ok:
        worker = await _check_worker(db, now, settings.worker_stale_seconds)
        rows = (await db.scalars(select(ProviderStatus).order_by(ProviderStatus.kind))).all()
        providers = [
            ProviderCheck(
                kind=r.kind,
                model=r.model,
                last_success_at=r.last_success_at,
                last_failure_at=r.last_failure_at,
                last_error_code=r.last_error_code,
                last_status_code=r.last_status_code,
                failing=_failing(r),
                failing_since=r.failing_since if _failing(r) else None,
            )
            for r in rows
        ]
    else:
        worker = WorkerCheck(status="unknown", stale_after_seconds=settings.worker_stale_seconds)
    disk = _check_disk(settings.disk_check_target, int(settings.disk_min_free_gb * GB))
    checks = HealthChecks(db=db_check, worker=worker, disk=disk, providers=providers)
    issues = build_issues(checks)
    tracker.annotate(issues, now)
    tracker.log_change(issues, checks)
    return HealthDetails(degraded=bool(issues), now=now, issues=issues, checks=checks)


Details = Annotated[HealthDetails, Depends(collect_health)]


@router.get("/health")
async def health(details: Details) -> PublicHealth:
    return PublicHealth(
        degraded=details.degraded, issues=[PublicIssue(code=i.code) for i in details.issues]
    )


@details_router.get("/health/details")
async def health_details(details: Details) -> HealthDetails:
    return details
