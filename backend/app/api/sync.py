"""Sync API (TRD 8.2). Agent 전용, Bearer 토큰(`require_sync_token`). CSRF 검사는 하지 않는다.

오류(`{"error": {"code", "message"}}`):
- 401 `invalid_token`: 토큰이 없거나 폐기됨
- 404 `run_not_found`, 409 `run_finished`(이미 끝난 실행에 하트비트·manifest·완료)
- 400 경로 검증 실패: `outside_root`, `fixed_exclude`, `excluded`, `relative_path`,
  `unsupported_extension`, `invalid_sha256`
- 413 `file_too_large`(`max_file_mb` 초과), 409 `hash_mismatch`(서버가 다시 계산한 해시가 다름)

로그 상관 키: 업로드·메타 등록은 선택 필드 `run_id`를 받아 로그에 남긴다(실행 단위 추적).
업로드 로그에는 `document_id`와 해시 앞 8자리(`sha256_prefix`)도 남긴다(경로·파일명 없음).
"""

import re
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Form, UploadFile
from pydantic import BaseModel, Field, field_validator

from app.api.deps import DbSession, require_sync_token
from app.api.errors import ApiError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import SyncRunSource
from app.db.models import SyncRun
from app.ingest.intake import FileMeta, accept_meta_only, accept_upload
from app.ingest.scope import FIXED_EXCLUDES, UPLOAD_EXTENSIONS
from app.jobs.runner import utc_now
from app.sync import runs, upload
from app.sync.manifest import judge_manifest
from app.sync.scope import SyncScope, load_scope

log = get_logger(__name__)

router = APIRouter(prefix="/sync", tags=["sync"], dependencies=[Depends(require_sync_token)])

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_SCOPE_MESSAGES = {
    "outside_root": "수집 루트 밖의 경로입니다",
    "fixed_exclude": "고정 제외 폴더(/project/, /backup/) 아래의 경로입니다",
    "excluded": "제외 패턴에 걸린 경로입니다",
    "relative_path": "절대 경로가 아닙니다",
    "unsupported_extension": "지원하지 않는 확장자입니다",
    "invalid_sha256": "SHA-256 값 형식이 아닙니다",
    "not_over_limit": "최대 크기 이하 파일은 업로드해야 합니다",
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class FileEntry(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    size: int = Field(ge=0)
    mtime: datetime
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _lower_sha(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("mtime")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware(v)

    def meta(self) -> FileMeta:
        return FileMeta(path=self.path, size=self.size, mtime=self.mtime, sha256=self.sha256)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


class ExcludeOut(BaseModel):
    pattern: str
    fixed: bool  # 고정 제외(웹에서 지울 수 없음, 허용 폴더 기준)


class SyncConfigOut(BaseModel):
    allowed_root: str
    roots: list[str]
    excludes: list[ExcludeOut]
    max_file_mb: int
    extensions: list[str]


@router.get("/config")
async def get_config(db: DbSession) -> SyncConfigOut:
    scope = await load_scope(db, get_settings())
    excludes = [ExcludeOut(pattern=p, fixed=True) for p in FIXED_EXCLUDES]
    excludes += [ExcludeOut(pattern=p, fixed=False) for p in scope.excludes]
    return SyncConfigOut(
        allowed_root=scope.allowed_root,
        roots=scope.roots,
        excludes=excludes,
        max_file_mb=scope.max_file_mb,
        extensions=sorted(UPLOAD_EXTENSIONS),
    )


# ---------------------------------------------------------------------------
# 실행 기록
# ---------------------------------------------------------------------------


class RunStartIn(BaseModel):
    source: SyncRunSource = SyncRunSource.SCHEDULE


class RunOut(BaseModel):
    run_id: int
    status: str
    started_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None


def _run_out(run: SyncRun) -> RunOut:
    return RunOut(
        run_id=run.id,
        status=run.status.value,
        started_at=run.started_at,
        heartbeat_at=run.heartbeat_at,
        finished_at=run.finished_at,
    )


async def _active_run_errors(coro: Awaitable[SyncRun]) -> SyncRun:
    try:
        return await coro
    except runs.RunNotFound as e:
        raise ApiError(404, "run_not_found", "동기화 실행을 찾을 수 없습니다") from e
    except runs.RunFinished as e:
        raise ApiError(
            409, "run_finished", "이미 끝난 동기화 실행입니다", extra={"status": e.status.value}
        ) from e


@router.post("/runs", status_code=201)
async def start_run(db: DbSession, body: RunStartIn | None = None) -> RunOut:
    source = body.source if body else SyncRunSource.SCHEDULE
    run = await runs.start_run(db, source, utc_now())
    await db.commit()
    return _run_out(run)


@router.post("/runs/{run_id}/heartbeat")
async def heartbeat(run_id: int, db: DbSession) -> RunOut:
    run = await _active_run_errors(runs.heartbeat(db, run_id, utc_now()))
    await db.commit()
    return _run_out(run)


class RunCompleteIn(BaseModel):
    status: Literal["succeeded", "failed"] = "succeeded"
    stats: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


@router.post("/runs/{run_id}/complete")
async def complete(run_id: int, body: RunCompleteIn, db: DbSession) -> RunOut:
    run = await _active_run_errors(
        runs.complete_run(
            db, run_id, status=body.status, stats=body.stats, error=body.error, now=utc_now()
        )
    )
    await db.commit()
    return _run_out(run)


class ManifestIn(BaseModel):
    files: list[FileEntry] = Field(default_factory=list)
    scan_errors: list[str] = Field(default_factory=list)


class RejectedOut(BaseModel):
    path: str
    reason: str


class ManifestOut(BaseModel):
    run_id: int
    upload: list[str]
    meta_only: list[str]
    deleted: list[str]
    restored: list[str]
    moved: list[dict[str, str]]
    copied: list[dict[str, str]]
    deletion_held: list[str]
    scan_error_held: list[str]
    rejected: list[RejectedOut]
    counts: dict[str, int]


@router.post("/runs/{run_id}/manifest")
async def manifest(run_id: int, body: ManifestIn, db: DbSession) -> ManifestOut:
    run = await _active_run_errors(runs.get_active_run(db, run_id))
    settings = get_settings()
    scope = await load_scope(db, settings)
    now = utc_now()
    result = await judge_manifest(
        db,
        run,
        [f.meta() for f in body.files],
        body.scan_errors,
        scope=scope,
        guard_ratio=settings.mass_delete_guard_ratio,
        now=now,
    )
    await db.commit()
    return ManifestOut(
        run_id=run.id,
        upload=result.upload,
        meta_only=result.meta_only,
        deleted=result.deleted,
        restored=result.restored,
        moved=result.moved,
        copied=result.copied,
        deletion_held=result.deletion_held,
        scan_error_held=result.scan_error_held,
        rejected=[RejectedOut(**r) for r in result.rejected],
        counts=result.counts(),
    )


# ---------------------------------------------------------------------------
# 업로드
# ---------------------------------------------------------------------------


def _reject(path_reason: str, run_id: int | None) -> ApiError:
    log.info("sync.file.rejected", reason=path_reason, run_id=run_id)
    return ApiError(400, path_reason, _SCOPE_MESSAGES.get(path_reason, "받을 수 없는 경로입니다"))


class UploadOut(BaseModel):
    document_id: int
    outcome: str


@router.put("/files")
async def upload_file(
    db: DbSession,
    path: Annotated[str, Form(min_length=1, max_length=4096)],
    sha256: Annotated[str, Form()],
    mtime: Annotated[datetime, Form()],
    file: UploadFile,
    run_id: Annotated[int | None, Form()] = None,
) -> UploadOut:
    """`run_id`(선택)는 로그 상관 키로만 쓴다. 업로드는 실행과 상관없이 받는다."""
    scope = await load_scope(db, get_settings())
    reason = scope.file_reason(path)
    if reason is not None:
        raise _reject(reason, run_id)
    expected = sha256.strip().lower()
    if not _SHA256.match(expected):
        raise _reject("invalid_sha256", run_id)
    keys = {"run_id": run_id, "sha256_prefix": expected[:8]}

    incoming = upload.incoming_path()
    try:
        try:
            actual, size = await upload.receive(file, incoming, max_bytes=scope.max_bytes)
        except upload.FileTooLarge as e:
            log.info(
                "sync.file.rejected",
                reason="file_too_large",
                max_file_mb=scope.max_file_mb,
                **keys,
            )
            raise ApiError(
                413,
                "file_too_large",
                f"{scope.max_file_mb}MB를 넘는 파일은 업로드하지 않고 파일명만 등록합니다",
            ) from e
        if actual != expected:
            log.warning("sync.file.hash_mismatch", size=size, **keys)
            raise ApiError(
                409,
                "hash_mismatch",
                "업로드한 내용의 해시가 manifest와 다릅니다. 다음 동기화에서 다시 올립니다",
            )
        meta = FileMeta(path=path, size=size, mtime=_aware(mtime), sha256=actual)
        result = await accept_upload(db, meta, incoming, move=True)
        await db.commit()
    finally:
        upload.discard(incoming)  # 옮겼으면 이미 없음. 실패·변경 없음이면 지운다
    log.info(
        "sync.file.uploaded",
        document_id=result.document_id,
        outcome=result.outcome,
        size=size,
        ext=meta.ext,
        **keys,
    )
    return UploadOut(document_id=result.document_id, outcome=result.outcome)


class MetaIn(BaseModel):
    files: list[FileEntry] = Field(default_factory=list)
    run_id: int | None = None  # 로그 상관 키(선택)


class MetaAccepted(BaseModel):
    path: str
    document_id: int
    outcome: str


class MetaOut(BaseModel):
    accepted: list[MetaAccepted]
    rejected: list[RejectedOut]


def _meta_reason(scope: SyncScope, entry: FileEntry) -> str | None:
    reason = scope.file_reason(entry.path)
    if reason is None and not _SHA256.match(entry.sha256):
        reason = "invalid_sha256"
    if reason is None and entry.size <= scope.max_bytes:
        reason = "not_over_limit"
    return reason


@router.post("/files/meta")
async def register_meta(body: MetaIn, db: DbSession) -> MetaOut:
    """`max_file_mb`를 넘는 파일: 원본 없이 파일명만 색인한다(`meta_only`)."""
    scope = await load_scope(db, get_settings())
    accepted: list[MetaAccepted] = []
    rejected: list[RejectedOut] = []
    for entry in body.files:
        reason = _meta_reason(scope, entry)
        if reason is not None:
            rejected.append(RejectedOut(path=entry.path, reason=reason))
            continue
        result = await accept_meta_only(db, entry.meta())
        accepted.append(
            MetaAccepted(path=entry.path, document_id=result.document_id, outcome=result.outcome)
        )
    await db.commit()
    log.info(
        "sync.file.meta_registered",
        run_id=body.run_id,
        accepted=len(accepted),
        meta_only=sum(1 for a in accepted if a.outcome == "meta_only"),
        rejected=len(rejected),
    )
    return MetaOut(accepted=accepted, rejected=rejected)
