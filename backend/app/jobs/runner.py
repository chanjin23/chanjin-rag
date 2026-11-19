"""작업 실행: extract → index → embed (TRD 5.1 상태도, 6.2, 6.3).

- `extract`: 임시 원본을 하위 프로세스에서 파싱(T04) → `sections` 저장 → 원본 삭제(실패해도).
  문서 상태 `extracting` → (`index` 등록) / `no_text` / `failed`.
- `index`: 청킹·토큰화(T05) → `chunks`를 한 트랜잭션에서 교체, 같은 문서의 이전 벡터 재사용.
  모두 채워지면 바로 `ready`, 아니면 `indexed` + `embed` 등록.
- `embed`: 하루 한도 안에서 남은 청크를 임베딩(32개씩 저장). `embedding` → `ready`,
  429가 계속되면 `indexed`로 반납.

- 결과를 저장하는 트랜잭션에서 문서 행을 잠그고 `jobs.sha256`과 문서의 현재 `sha256`을 비교한다.
  다르면(처리 중에 새 버전이 올라옴) 결과를 버리고 작업을 `done`으로 끝낸다(`job.discarded_stale`).
  가져갈 때도 같은 검사를 해서, 이미 지난 버전의 `embed`는 한도를 차감하지 않고 버린다.
- `no_text`, `unsupported_format`은 `extract`에서 바로 파일명 청크 1개를 만들고 끝낸다.
- 동시성(TRD 12.5): 파싱은 한 번에 하나(`_parse_lock`),
  임베딩 API 호출은 동시에 두 개(`_embed_slots`).
- 로그: `job.started`, `job.completed`, `job.failed`, `job.discarded_stale`, `quota.exhausted`.
  경로·파일명·본문은 남기지 않는다(문서 ID만).
"""

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.enums import DocumentStatus, FailureCode, JobStatus, JobType, SectionKind
from app.db.models import Chunk, Document, Job, Section
from app.ingest.chunking import build_chunks, embed_input
from app.ingest.intake import (
    chunk_row,
    original_path,
    remove_original_version,
    replace_with_filename_chunk,
)
from app.ingest.parsers.base import ParseResult
from app.ingest.parsers.base import Section as SectionData
from app.ingest.parsers.isolation import parse_in_subprocess
from app.jobs import queue, quota
from app.jobs.quota import QuotaExhausted, QuotaReason
from app.providers.base import EmbeddingProvider
from app.providers.errors import EmbeddingDimensionError, ProviderAuthError, RateLimited

log = get_logger(__name__)

# embed 작업이 한 번에 저장하는 청크 수. 저장할 때마다 버전을 확인하고 locked_at을 갱신한다.
EMBED_SAVE_GROUP = 32
# 임베딩 API 동시 호출 수(TRD 12.5)
EMBED_CONCURRENCY = 2

PREP_TYPES: tuple[JobType, ...] = (JobType.EXTRACT, JobType.INDEX)
EMBED_TYPES: tuple[JobType, ...] = (JobType.EMBED,)

Clock = Callable[[], datetime]
ParseFn = Callable[..., ParseResult]
Outcome = Literal["completed", "discarded", "released", "failed", "lost"]

# 오래 기다려야 하는 Provider 오류: 429가 계속됨, 키 오류, 차원 설정 오류(TRD 6.2)
_SLOW_RETRY_ERRORS = (RateLimited, ProviderAuthError, EmbeddingDimensionError)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ClaimedJob:
    """가져간 작업과 그때의 문서 정보(트랜잭션 밖에서 쓰는 스냅샷)."""

    id: int
    type: JobType
    document_id: int
    sha256: str
    attempts: int
    filename: str
    path: str
    ext: str
    charged: bool = False  # embed: 이번에 한도를 차감했는지


@dataclass
class RunSummary:
    """`run_until_idle` 결과. 작업 종류·결과별 개수."""

    counts: dict[str, int] = field(default_factory=dict)
    quota_exhausted: QuotaExhausted | None = None

    def add(self, job: ClaimedJob, outcome: Outcome) -> None:
        key = f"{job.type.value}.{outcome}"
        self.counts[key] = self.counts.get(key, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class StaleVersion(Exception):
    """저장 시점에 문서의 현재 버전이 작업 버전과 다름."""


class JobRunner:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        embedder: EmbeddingProvider,
        *,
        settings: Settings | None = None,
        clock: Clock = utc_now,
        parse: ParseFn = parse_in_subprocess,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        self.settings = settings or get_settings()
        self.clock = clock
        self.parse = parse
        self._parse_lock = asyncio.Lock()
        self._embed_slots = asyncio.Semaphore(EMBED_CONCURRENCY)
        self._exhausted_logged: set[tuple[date, QuotaReason]] = set()

    # ------------------------------------------------------------------ 큐

    async def requeue_stale(self) -> list[int]:
        stale_after = timedelta(minutes=self.settings.job_stale_lock_minutes)
        async with self.sessionmaker() as s, s.begin():
            ids = await queue.requeue_stale(s, now=self.clock(), stale_after=stale_after)
        if ids:
            log.warning("job.requeued_stale", job_ids=ids, count=len(ids))
        return ids

    async def claim(self, types: Sequence[JobType]) -> ClaimedJob | None:
        """extract·index 작업 하나를 가져간다. 지난 버전 작업은 버리고 다음 것을 본다."""
        now = self.clock()
        async with self.sessionmaker() as s, s.begin():
            while True:
                job = await queue.lock_next(s, types, now)
                if job is None:
                    return None
                doc = await s.get(Document, job.document_id)
                if doc is None or doc.sha256 != job.sha256:
                    self._discard(job, now, stage="claim")
                    continue
                queue.mark_running(job, now)
                if job.type == JobType.EXTRACT:
                    doc.status = DocumentStatus.EXTRACTING
                return self._snapshot(job, doc)

    async def claim_embed(self) -> ClaimedJob | QuotaExhausted | None:
        """embed 작업 하나를 한도 안에서 가져간다(TRD 6.2 하루 임베딩 한도).

        - 이미 차감한 버전(`embed_charged_sha256` = 작업 `sha256`)은 다시 세지 않는다.
        - 차감이 필요하면 오늘 `used` < `EMBED_DAILY_FILE_LIMIT`일 때만 1 올린다. 한도에 닿으면
          이미 차감한 버전의 작업만 가져가고, 그것도 없으면 `QuotaExhausted`(다음 KST 자정까지).
        - 하루 청크 상한(`EMBED_DAILY_CHUNK_LIMIT`)에 닿으면 새 embed를 모두 쉰다.
        """
        now = self.clock()
        async with self.sessionmaker() as s, s.begin():
            row = await quota.lock_quota(s, quota.quota_day(now))
            if quota.chunk_limit_reached(row):
                return self._exhausted(quota.exhausted(row, "chunks", now))
            files_full = quota.file_limit_reached(row)
            while True:
                stmt = queue.ready_jobs(EMBED_TYPES, now)
                if files_full:  # 한도가 찼으면 이미 차감한 버전(429로 반납된 것 등)만 가져간다
                    stmt = stmt.join(Document, Document.id == Job.document_id).where(
                        Document.embed_charged_sha256 == Job.sha256
                    )
                job = await s.scalar(stmt.limit(1).execution_options(populate_existing=True))
                if job is None:
                    if files_full and await self._has_ready(s, EMBED_TYPES, now):
                        return self._exhausted(quota.exhausted(row, "files", now))
                    return None
                doc = await s.get(Document, job.document_id)
                if doc is None or doc.sha256 != job.sha256:
                    self._discard(job, now, stage="claim")
                    continue
                charged = doc.embed_charged_sha256 != job.sha256
                if charged:
                    row.used += 1
                    doc.embed_charged_sha256 = job.sha256
                queue.mark_running(job, now)
                doc.status = DocumentStatus.EMBEDDING
                return self._snapshot(job, doc, charged=charged)

    async def _has_ready(self, s: AsyncSession, types: Sequence[JobType], now: datetime) -> bool:
        count = await s.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.status == JobStatus.QUEUED, Job.type.in_(list(types)), Job.run_after <= now)
        )
        return bool(count)

    def _exhausted(self, info: QuotaExhausted) -> QuotaExhausted:
        key = (info.day, info.reason)
        if key not in self._exhausted_logged:  # 같은 날·같은 이유는 한 번만 남긴다
            self._exhausted_logged.add(key)
            log.warning(
                "quota.exhausted",
                reason=info.reason,
                day=info.day.isoformat(),
                used=info.used,
                limit=info.limit,
                resume_at=info.resume_at.isoformat(),
            )
        return info

    @staticmethod
    def _snapshot(job: Job, doc: Document, *, charged: bool = False) -> ClaimedJob:
        return ClaimedJob(
            id=job.id,
            type=job.type,
            document_id=job.document_id,
            sha256=job.sha256,
            attempts=job.attempts,
            filename=doc.filename,
            path=doc.path,
            ext=doc.ext,
            charged=charged,
        )

    @staticmethod
    def _discard(job: Job, now: datetime, *, stage: str) -> None:
        queue.mark_done(job, now)
        job.last_error = "버전이 바뀌어 결과를 버림"
        if job.type == JobType.EXTRACT:  # 실행되지 않은 지난 버전의 임시 원본도 지운다
            remove_original_version(job.document_id, job.sha256)
        log.info(
            "job.discarded_stale",
            job_id=job.id,
            type=job.type.value,
            document_id=job.document_id,
            stage=stage,
        )

    # ------------------------------------------------------------------ 실행

    async def run(self, job: ClaimedJob) -> Outcome:
        """가져간 작업 하나를 실행한다. 예외는 밖으로 내보내지 않고 반납·실패로 기록한다."""
        started = time.monotonic()
        log.info(
            "job.started",
            job_id=job.id,
            type=job.type.value,
            document_id=job.document_id,
            attempt=job.attempts,
            **({"quota_charged": job.charged} if job.type == JobType.EMBED else {}),
        )
        try:
            if job.type == JobType.EXTRACT:
                fields = await self._extract(job)
            elif job.type == JobType.INDEX:
                fields = await self._index(job)
            else:
                fields = await self._embed(job)
        except StaleVersion:
            await self._finish_discarded(job)
            return "discarded"
        except queue.JobGone:
            log.info(
                "job.discarded_stale",
                job_id=job.id,
                type=job.type.value,
                document_id=job.document_id,
                stage="deleted",
            )
            return "discarded"
        except Exception as e:  # noqa: BLE001 - 작업 하나의 실패가 Worker를 멈추지 않게
            return await self._handle_error(job, e)
        log.info(
            "job.completed",
            job_id=job.id,
            type=job.type.value,
            document_id=job.document_id,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            **fields,
        )
        return "completed"

    async def run_next(self, types: Sequence[JobType]) -> ClaimedJob | None:
        """extract·index 작업 하나를 가져가 실행한다. 없으면 None."""
        job = await self.claim(types)
        if job is not None:
            await self.run(job)
        return job

    async def run_until_idle(self, *, max_jobs: int | None = None) -> RunSummary:
        """지금 처리할 수 있는 작업을 모두 처리한다(CLI·테스트용). 한도에 걸리면 멈춘다."""
        summary = RunSummary()
        await self.requeue_stale()
        while max_jobs is None or summary.total < max_jobs:
            job = await self.claim(PREP_TYPES)
            if job is None:
                claimed = await self.claim_embed()
                if isinstance(claimed, QuotaExhausted):
                    summary.quota_exhausted = claimed
                    break
                job = claimed
            if job is None:
                break
            summary.add(job, await self.run(job))
        return summary

    # ------------------------------------------------------------------ 공통 저장

    async def _lock_current(self, s: AsyncSession, job: ClaimedJob) -> tuple[Job, Document]:
        """저장 트랜잭션 시작: 작업·문서를 잠그고 버전을 확인한다."""
        job_row = await queue.lock_job(s, job.id)
        doc = await s.scalar(
            select(Document)
            .where(Document.id == job.document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if doc is None:
            raise queue.JobGone(job.id)
        if doc.sha256 != job.sha256:
            raise StaleVersion
        return job_row, doc

    async def _finish_discarded(self, job: ClaimedJob) -> None:
        now = self.clock()
        async with self.sessionmaker() as s, s.begin():
            row = await s.get(Job, job.id, with_for_update=True)
            if row is not None:
                self._discard(row, now, stage="save")

    async def _handle_error(self, job: ClaimedJob, error: Exception) -> Outcome:
        now = self.clock()
        message = f"{type(error).__name__}: {error}"
        final = job.attempts >= self.settings.job_max_attempts
        if isinstance(error, _SLOW_RETRY_ERRORS):
            delay = timedelta(minutes=self.settings.job_rate_limit_delay_minutes)
        else:  # 1, 2, 4, … 분(최대 30분)
            delay = timedelta(minutes=min(2 ** (job.attempts - 1), 30))
        retry_at = now + delay
        async with self.sessionmaker() as s, s.begin():
            row = await s.get(Job, job.id, with_for_update=True)
            if row is None:
                return "lost"
            doc = await s.get(Document, job.document_id, with_for_update=True)
            current = doc is not None and doc.sha256 == job.sha256
            if final:
                queue.mark_failed(row, now, error=message)
                if current and doc is not None:
                    doc.status = DocumentStatus.FAILED
                    if job.type == JobType.EXTRACT:
                        doc.failure_code = FailureCode.PARSER_ERROR
                    doc.failure_detail = message[:500]
            else:
                queue.mark_released(row, run_after=retry_at, error=message)
                if current and doc is not None and job.type == JobType.EMBED:
                    # 상태도(TRD 5.1): embedding → indexed(일시 오류, 재시도 대기)
                    doc.status = DocumentStatus.INDEXED
        log.warning(
            "job.failed",
            job_id=job.id,
            type=job.type.value,
            document_id=job.document_id,
            attempt=job.attempts,
            error=type(error).__name__,
            status_code=getattr(error, "status_code", None),
            final=final,
            retry_at=None if final else retry_at.isoformat(),
        )
        return "failed" if final else "released"

    # ------------------------------------------------------------------ extract

    async def _extract(self, job: ClaimedJob) -> dict[str, Any]:
        src = original_path(job.document_id, job.sha256, job.ext)
        missing = not src.exists()
        try:
            if missing:
                result = ParseResult.failed(
                    FailureCode.PARSER_ERROR, "임시 원본이 없음(다음 동기화 때 다시 올림)"
                )
            else:
                async with self._parse_lock:  # 파싱은 한 번에 하나(TRD 12.5)
                    result = await asyncio.to_thread(
                        self.parse, src, document_id=job.document_id, job_id=job.id
                    )
        finally:
            _delete_original(src, job)

        now = self.clock()
        async with self.sessionmaker() as s, s.begin():
            job_row, doc = await self._lock_current(s, job)
            await s.execute(delete(Section).where(Section.document_id == doc.id))
            if result.status == "ok":
                s.add_all(_section_rows(doc.id, result.sections))
                doc.status = DocumentStatus.EXTRACTING
                await queue.enqueue(s, type=JobType.INDEX, document=doc, run_after=now)
            elif result.status == "no_text":
                doc.status = DocumentStatus.NO_TEXT
                await replace_with_filename_chunk(s, doc)
            else:
                doc.status = DocumentStatus.FAILED
                doc.failure_code = result.failure_code
                doc.failure_detail = result.failure_detail
                doc.needs_upload = missing
                if result.failure_code == FailureCode.UNSUPPORTED_FORMAT:
                    # 내용은 못 읽어도 파일명으로는 찾을 수 있게 한다(TRD 6.3)
                    await replace_with_filename_chunk(s, doc)
            queue.mark_done(job_row, now)
        return {
            "status": doc.status.value,
            "sections": len(result.sections),
            "chars": result.text_chars,
            "warnings": len(result.warnings),
            **({"failure_code": result.failure_code.value} if result.failure_code else {}),
        }

    # ------------------------------------------------------------------ index

    async def _index(self, job: ClaimedJob) -> dict[str, Any]:
        async with self.sessionmaker() as s:
            rows = (
                await s.scalars(
                    select(Section)
                    .where(Section.document_id == job.document_id)
                    .order_by(Section.seq)
                )
            ).all()
            sections = [
                SectionData(
                    location=dict(r.location),
                    text=r.text,
                    kind=r.kind.value,
                    heading_path=list(r.heading_path),
                )
                for r in rows
            ]
        chunks = await asyncio.to_thread(
            build_chunks, sections, filename=job.filename, path=job.path
        )

        model = self.embedder.model
        now = self.clock()
        async with self.sessionmaker() as s, s.begin():
            job_row, doc = await self._lock_current(s, job)
            # 같은 문서의 이전 청크 벡터(같은 임베딩 입력 해시·같은 모델)를 남겨 두었다가 재사용
            previous = {
                sha: vec
                for sha, vec in (
                    await s.execute(
                        select(Chunk.content_sha256, Chunk.embedding).where(
                            Chunk.document_id == doc.id,
                            Chunk.embedding.is_not(None),
                            Chunk.embed_model == model,
                        )
                    )
                ).all()
            }
            await s.execute(delete(Chunk).where(Chunk.document_id == doc.id))
            reused = missing = 0
            for data in chunks:
                row = chunk_row(doc, data)
                vec = previous.get(data.content_sha256) if data.embeddable else None
                if vec is not None:
                    row.embedding = vec
                    row.embed_model = model
                    reused += 1
                elif data.embeddable:
                    missing += 1
                s.add(row)
            await s.flush()
            doc.content_indexed = True
            if missing == 0:
                doc.status = DocumentStatus.READY
                doc.embed_model = model
                doc.embed_dim = self.embedder.dim
            else:
                doc.status = DocumentStatus.INDEXED
                await queue.enqueue(s, type=JobType.EMBED, document=doc, run_after=now)
            queue.mark_done(job_row, now)
        return {
            "status": doc.status.value,
            "chunks": len(chunks),
            "tokens": sum(c.token_count for c in chunks),
            "reused": reused,
            "to_embed": missing,
        }

    # ------------------------------------------------------------------ embed

    async def _embed(self, job: ClaimedJob) -> dict[str, Any]:
        embedded = 0
        while True:
            async with self.sessionmaker() as s:
                group = (
                    await s.execute(
                        select(Chunk.id, Chunk.filename, Chunk.heading_path, Chunk.content)
                        .where(Chunk.document_id == job.document_id, Chunk.embedding.is_(None))
                        .order_by(Chunk.seq)
                        .limit(EMBED_SAVE_GROUP)
                    )
                ).all()
            if not group:
                break
            texts = [embed_input(fn, hp, content) for _, fn, hp, content in group]
            vectors = await self._embed_texts(texts)
            now = self.clock()
            async with self.sessionmaker() as s, s.begin():
                # API는 이미 불렀으므로 결과를 버리더라도 청크 사용량은 센다
                await quota.add_chunks_used(s, now, len(texts))
            async with self.sessionmaker() as s, s.begin():
                await self._lock_current(s, job)
                for (chunk_id, *_), vec in zip(group, vectors, strict=True):
                    await s.execute(
                        update(Chunk)
                        .where(Chunk.id == chunk_id, Chunk.embedding.is_(None))
                        .values(embedding=vec, embed_model=self.embedder.model)
                    )
                await queue.touch(s, job.id, now)
            embedded += len(group)

        now = self.clock()
        async with self.sessionmaker() as s, s.begin():
            job_row, doc = await self._lock_current(s, job)
            doc.status = DocumentStatus.READY
            doc.embed_model = self.embedder.model
            doc.embed_dim = self.embedder.dim
            queue.mark_done(job_row, now)
        return {"status": DocumentStatus.READY.value, "embedded": embedded}

    async def _embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """`EMBED_BATCH_SIZE`개씩 나눠 동시에 최대 2개 요청한다. 입력 순서대로 돌려준다."""
        size = max(1, self.settings.embed_batch_size)
        batches = [list(texts[i : i + size]) for i in range(0, len(texts), size)]

        async def call(batch: list[str]) -> list[list[float]]:
            async with self._embed_slots:
                return await self.embedder.embed(batch, input_type="passage")

        results = await asyncio.gather(*(call(b) for b in batches))
        return [vec for batch in results for vec in batch]


def _section_rows(document_id: int, sections: Sequence[SectionData]) -> list[Section]:
    return [
        Section(
            document_id=document_id,
            seq=i,
            location=sec.location,
            heading_path=list(sec.heading_path),
            kind=SectionKind(sec.kind),
            text=sec.text,
        )
        for i, sec in enumerate(sections)
    ]


def _delete_original(src: Path, job: ClaimedJob) -> None:
    """임시 원본 삭제. 실패해도 작업은 계속한다(로그만)."""
    try:
        src.unlink(missing_ok=True)
    except OSError as e:
        log.warning(
            "job.original_delete_failed",
            job_id=job.id,
            document_id=job.document_id,
            error=type(e).__name__,
        )
