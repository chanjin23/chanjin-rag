"""로컬 폴더 수집 `rag-backend ingest-local <폴더>` (개발·E2E 시드 데이터용).

Agent 없이 로컬 폴더의 파일을 업로드와 같은 경로(`intake.accept_upload`)로 큐에 넣고,
원하면 그 자리에서 Worker와 같은 `JobRunner`로 처리한다.

- 범위 검증은 Sync와 같다: `SYNC_ALLOWED_ROOT` 밖이거나 고정 제외(`/project/`, `/backup/`) 아래면
  거부한다. 테스트·E2E는 `SYNC_ALLOWED_ROOT`를 임시 폴더로 바꿔 쓴다.
- 파일마다 제외 패턴(`sync_config.excludes`), 지원 확장자(TRD 6.1), 크기(`max_file_mb` 초과는
  `meta_only`)를 적용한다. 심볼릭 링크가 범위 밖을 가리키면 따라가지 않는다.
- 로그: `ingest.local.started`, `ingest.local.completed`(개수만, 경로·파일명 없음).
"""

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Document, SyncConfig
from app.ingest.intake import FileMeta, accept_meta_only, accept_upload
from app.ingest.scope import ScopeReason, check_scope, is_upload_extension
from app.jobs.runner import JobRunner, RunSummary

log = get_logger(__name__)

_HASH_CHUNK = 1024 * 1024


class OutOfScopeError(ValueError):
    def __init__(self, reason: ScopeReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class LocalIngestReport:
    queued: int = 0
    unchanged: int = 0
    meta_only: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    document_ids: list[int] = field(default_factory=list)
    run: RunSummary | None = None
    statuses: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(_HASH_CHUNK):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class _Candidate:
    meta: FileMeta
    source: Path  # 실제로 읽을 파일(심볼릭 링크면 대상)
    meta_only: bool


def _scan(
    folder: Path, root: str, excludes: list[str], max_bytes: int, report: LocalIngestReport
) -> list[_Candidate]:
    """폴더를 돌며 수집할 파일과 해시를 구한다(블로킹 I/O라 스레드에서 실행)."""
    found: list[_Candidate] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        # 제외 폴더는 내려가지 않는다(폴더 아래 가상의 파일로 검사)
        kept = []
        for d in sorted(dirnames):
            if check_scope(os.path.join(dirpath, d, "_"), root, excludes) is None:
                kept.append(d)
            else:
                report.skip("excluded_dir")
        dirnames[:] = kept
        for name in sorted(filenames):
            file = Path(dirpath) / name
            target = file.resolve()
            why = check_scope(str(file), root, excludes) or check_scope(str(target), root, excludes)
            if why is not None:
                report.skip(why)
                continue
            if not is_upload_extension(name):
                report.skip("unsupported_extension")
                continue
            try:
                st = target.stat()
                meta = FileMeta(
                    path=str(file),
                    size=st.st_size,
                    mtime=datetime.fromtimestamp(st.st_mtime, UTC),
                    sha256=file_sha256(target),
                )
            except OSError:
                report.skip("unreadable")
                continue
            found.append(_Candidate(meta, target, meta_only=meta.size > max_bytes))
    return found


def _resolve_folder(folder: Path, root: str) -> Path:
    resolved = folder.resolve()
    reason = check_scope(str(resolved), root)
    if reason is not None or not resolved.is_dir():
        raise OutOfScopeError(reason or "relative_path")
    return resolved


async def ingest_local(
    sessionmaker: async_sessionmaker[AsyncSession],
    folder: Path,
    *,
    runner: JobRunner | None = None,
) -> LocalIngestReport:
    """`folder`의 파일을 접수하고, `runner`가 있으면 처리할 수 있는 작업을 모두 처리한다."""
    root = get_settings().sync_allowed_root
    folder = await asyncio.to_thread(_resolve_folder, folder, root)

    async with sessionmaker() as s:
        config = await s.get(SyncConfig, 1)
    excludes = list(config.excludes) if config else []
    max_bytes = (config.max_file_mb if config else 10) * 1024 * 1024

    report = LocalIngestReport()
    candidates = await asyncio.to_thread(_scan, folder, root, excludes, max_bytes, report)
    log.info("ingest.local.started", files=len(candidates), process=runner is not None)
    for c in candidates:
        async with sessionmaker() as s, s.begin():
            if c.meta_only:
                result = await accept_meta_only(s, c.meta)
            else:
                result = await accept_upload(s, c.meta, c.source)
        report.document_ids.append(result.document_id)
        if result.outcome == "queued":
            report.queued += 1
        elif result.outcome == "meta_only":
            report.meta_only += 1
        else:
            report.unchanged += 1

    if runner is not None:
        report.run = await runner.run_until_idle()
    if report.document_ids:
        async with sessionmaker() as s:
            rows = await s.execute(
                select(Document.status, func.count())
                .where(Document.id.in_(report.document_ids))
                .group_by(Document.status)
            )
            report.statuses = {status.value: count for status, count in rows.all()}
    log.info(
        "ingest.local.completed",
        queued=report.queued,
        unchanged=report.unchanged,
        meta_only=report.meta_only,
        skipped=report.skipped,
        jobs=report.run.counts if report.run else {},
        quota_exhausted=bool(report.run and report.run.quota_exhausted),
        statuses=report.statuses,
    )
    return report
