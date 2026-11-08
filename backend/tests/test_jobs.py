"""작업 큐와 Ingestion Worker (TRD 6.2, 6.3, 14장 통합 테스트).

Worker는 작업마다 자기 트랜잭션으로 commit하므로, 이 파일의 테스트는 롤백 세션 대신
실제로 commit하는 엔진을 쓰고 앞뒤로 수집 테이블을 비운다.
시계(`Clock`)를 주입해 30분 뒤·다음 날 자정을 흉내 낸다.
문서는 모두 `tmp_path`에 만든 합성 파일이다. 모델은 Fake 임베딩(외부 호출 없음).
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.db.engine import create_engine, create_sessionmaker
from app.db.enums import DocumentStatus, FailureCode, JobStatus, JobType
from app.db.models import EMBED_DIM, Chunk, Document, EmbedQuota, Job, Section
from app.ingest.intake import FileMeta, accept_upload, original_path
from app.ingest.parsers import parse_document
from app.ingest.parsers.base import ParseResult
from app.jobs import queue
from app.jobs.quota import QuotaExhausted, quota_day
from app.jobs.runner import PREP_TYPES, ClaimedJob, JobRunner
from app.providers.base import InputType
from app.providers.errors import ProviderUnavailable, RateLimited
from app.providers.fake import FakeEmbeddingProvider

KST = ZoneInfo("Asia/Seoul")
SM = async_sessionmaker[AsyncSession]


# ---------------------------------------------------------------------------
# 픽스처와 도우미
# ---------------------------------------------------------------------------


class Clock:
    """주입하는 시계(멈춰 있음). 실제 시각보다 1분 앞에서 시작해, 접수(DB now())로 만든 작업의
    `run_after`가 테스트 도중에도 이 시계보다 앞서게 한다."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC) + timedelta(minutes=1)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class CountingEmbedder:
    """Fake 임베딩을 감싸 호출을 센다.

    `failures`의 예외를 차례로 던지고, `before`는 호출 직전에 부르는 훅이다.
    """

    inner: FakeEmbeddingProvider = field(default_factory=lambda: FakeEmbeddingProvider(EMBED_DIM))
    failures: list[Exception] = field(default_factory=list)
    before: Callable[[], Awaitable[None]] | None = None
    texts: list[str] = field(default_factory=list)

    @property
    def model(self) -> str:
        return self.inner.model

    @property
    def dim(self) -> int:
        return self.inner.dim

    async def embed(self, texts: Sequence[str], *, input_type: InputType) -> list[list[float]]:
        assert input_type == "passage"
        if self.before is not None:
            await self.before()
        if self.failures:
            raise self.failures.pop(0)
        self.texts.extend(texts)
        return await self.inner.embed(texts, input_type=input_type)


def in_process_parse(path: Path, **_: Any) -> ParseResult:
    """테스트 속도를 위해 같은 프로세스에서 파싱한다(하위 프로세스 격리는 T04 테스트가 확인)."""
    return parse_document(path)


async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM documents"))  # sections·chunks·jobs는 CASCADE
        await conn.execute(text("DELETE FROM embed_quota"))
        await conn.execute(text("DELETE FROM worker_heartbeats"))


@pytest.fixture
async def sm(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SM]:
    monkeypatch.setenv("UPLOAD_TMP_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("SYNC_ALLOWED_ROOT", str(tmp_path / "docs"))
    get_settings.cache_clear()
    engine = create_engine(migrated_db, poolclass=NullPool)
    await _clean(engine)
    try:
        yield create_sessionmaker(engine)
    finally:
        await _clean(engine)
        await engine.dispose()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def embedder() -> CountingEmbedder:
    return CountingEmbedder()


@pytest.fixture
def runner(sm: SM, clock: Clock, embedder: CountingEmbedder) -> JobRunner:
    return JobRunner(sm, embedder, clock=clock, parse=in_process_parse)


def set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


async def upload(
    sm: SM, tmp_path: Path, name: str, content: str | bytes, *, mtime: datetime | None = None
) -> int:
    """`tmp_path/docs/name`에 파일을 쓰고 업로드와 같은 방식으로 접수한다. 문서 ID."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    file = tmp_path / "docs" / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)
    meta = FileMeta(
        path=str(file),
        size=len(data),
        mtime=mtime or datetime.now(UTC),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    async with sm() as s, s.begin():
        result = await accept_upload(s, meta, file)
    return result.document_id


async def doc(sm: SM, document_id: int) -> Document:
    async with sm() as s:
        d = await s.get(Document, document_id)
        assert d is not None
        return d


async def jobs_of(sm: SM, document_id: int) -> list[Job]:
    async with sm() as s:
        return list(
            await s.scalars(select(Job).where(Job.document_id == document_id).order_by(Job.id))
        )


async def chunks_of(sm: SM, document_id: int) -> list[Chunk]:
    async with sm() as s:
        return list(
            await s.scalars(
                select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.seq)
            )
        )


async def quota_row(sm: SM, clock: Callable[[], datetime]) -> EmbedQuota | None:
    async with sm() as s:
        return await s.get(EmbedQuota, quota_day(clock()))


async def run_prep(runner: JobRunner) -> int:
    """extract·index 작업을 모두 처리한다. 처리한 작업 수."""
    count = 0
    while await runner.run_next(PREP_TYPES) is not None:
        count += 1
    return count


async def claim_embed_job(runner: JobRunner) -> ClaimedJob:
    claimed = await runner.claim_embed()
    assert isinstance(claimed, ClaimedJob), claimed
    return claimed


def events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == name]


MD_THREE_SECTIONS = (
    "# 계약\n\n위약금 조항은 계약 해지 시 적용한다.\n\n"
    "# 일정\n\n납품 일정은 3월 둘째 주로 정했다.\n\n"
    "# 비용\n\n총 비용은 1,200만 원이며 분할 지급한다.\n"
)


# ---------------------------------------------------------------------------
# 큐
# ---------------------------------------------------------------------------


async def test_skip_locked_single_consumer(
    sm: SM, tmp_path: Path, runner: JobRunner, clock: Clock
) -> None:
    # 1) 한 트랜잭션이 잠근 작업은 다른 트랜잭션이 가져가지 못한다(SKIP LOCKED)
    only = await upload(sm, tmp_path, "only.txt", "하나뿐인 작업")
    async with sm() as a, a.begin():
        locked = await queue.lock_next(a, PREP_TYPES, clock())
        assert locked is not None and locked.document_id == only
        async with sm() as b, b.begin():
            assert await queue.lock_next(b, PREP_TYPES, clock()) is None
    # 잠금이 풀리면 다시 보인다
    async with sm() as b, b.begin():
        assert await queue.lock_next(b, PREP_TYPES, clock()) is not None

    # 2) 여러 소비자가 동시에 가져가도 작업마다 정확히 한 번만 나간다
    for i in range(5):
        await upload(sm, tmp_path, f"doc{i}.txt", f"문서 {i} 본문")

    claimed: list[int] = []

    async def consumer() -> None:
        while (job := await runner.claim(PREP_TYPES)) is not None:
            claimed.append(job.id)

    await asyncio.gather(*(consumer() for _ in range(8)))
    assert len(claimed) == 6
    assert len(set(claimed)) == 6
    async with sm() as s:
        rows = (await s.scalars(select(Job))).all()
    assert {j.status for j in rows} == {JobStatus.RUNNING}
    assert all(j.attempts == 1 and j.locked_at == clock() for j in rows)


async def test_stale_lock_requeued(sm: SM, tmp_path: Path, runner: JobRunner, clock: Clock) -> None:
    stuck_doc = await upload(sm, tmp_path, "stuck.txt", "멈춘 작업")
    stuck = await runner.claim(PREP_TYPES)
    assert stuck is not None and stuck.document_id == stuck_doc

    clock.advance(minutes=9)
    fresh_doc = await upload(sm, tmp_path, "fresh.txt", "막 시작한 작업")
    fresh = await runner.claim(PREP_TYPES)
    assert fresh is not None and fresh.document_id == fresh_doc

    # 10분이 안 지났으면 그대로
    assert await runner.requeue_stale() == []
    clock.advance(minutes=2)  # stuck은 11분, fresh는 2분
    with capture_logs() as logs:
        assert await runner.requeue_stale() == [stuck.id]
    assert events(logs, "job.requeued_stale")[0]["job_ids"] == [stuck.id]

    by_id = {j.id: j for j in await jobs_of(sm, stuck_doc) + await jobs_of(sm, fresh_doc)}
    assert by_id[stuck.id].status == JobStatus.QUEUED
    assert by_id[stuck.id].locked_at is None
    assert by_id[fresh.id].status == JobStatus.RUNNING

    # 다시 가져가 끝까지 처리할 수 있다(시도 횟수는 이어서 센다)
    again = await runner.claim(PREP_TYPES)
    assert again is not None and again.id == stuck.id and again.attempts == 2
    assert await runner.run(again) == "completed"


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


async def test_extract_deletes_original_even_on_failure(
    sm: SM, tmp_path: Path, clock: Clock, embedder: CountingEmbedder
) -> None:
    # 1) 깨진 PDF: 실제 하위 프로세스 파싱이 실패해도 원본은 지우고 failed + 사유를 남긴다
    runner = JobRunner(sm, embedder, clock=clock)  # parse = parse_in_subprocess
    broken = await upload(sm, tmp_path, "broken.pdf", b"%PDF-1.7 this is not really a pdf")
    d = await doc(sm, broken)
    src = original_path(broken, d.sha256, ".pdf")
    assert src.exists()
    with capture_logs() as logs:
        assert await run_prep(runner) == 1
    assert not src.exists()
    d = await doc(sm, broken)
    assert d.status == DocumentStatus.FAILED
    assert d.failure_code == FailureCode.CORRUPTED
    assert [j.status for j in await jobs_of(sm, broken)] == [JobStatus.DONE]
    completed = events(logs, "job.completed")
    assert completed[0]["failure_code"] == "corrupted" and completed[0]["status"] == "failed"

    # 2) 파서가 예외를 던져도(예상 못 한 오류) 원본은 지운다. 작업은 재시도로 반납된다
    def exploding_parse(path: Path, **_: Any) -> ParseResult:
        raise RuntimeError("parser crashed")

    runner = JobRunner(sm, embedder, clock=clock, parse=exploding_parse)
    crash = await upload(sm, tmp_path, "crash.txt", "본문")
    src = original_path(crash, (await doc(sm, crash)).sha256, ".txt")
    with capture_logs() as logs:
        job = await runner.claim(PREP_TYPES)
        assert job is not None
        assert await runner.run(job) == "released"
    assert not src.exists()
    failed = events(logs, "job.failed")
    assert failed[0]["error"] == "RuntimeError" and failed[0]["final"] is False
    (row,) = await jobs_of(sm, crash)
    assert row.status == JobStatus.QUEUED and row.run_after > clock()

    # 다시 돌면 원본이 없으므로 failed + 다음 동기화 때 다시 올리도록 표시
    clock.advance(minutes=2)
    assert await run_prep(runner) == 1
    d = await doc(sm, crash)
    assert d.status == DocumentStatus.FAILED
    assert d.failure_code == FailureCode.PARSER_ERROR
    assert d.needs_upload is True
    assert list((tmp_path / "uploads").iterdir()) == []


async def test_no_text_document_state(
    sm: SM, tmp_path: Path, runner: JobRunner, embedder: CountingEmbedder
) -> None:
    empty = await upload(sm, tmp_path, "빈메모.txt", "  \n\n \t\n")
    await run_prep(runner)
    d = await doc(sm, empty)
    assert d.status == DocumentStatus.NO_TEXT
    assert d.content_indexed is True
    # 파일명 청크 1개(BM25 전용, 임베딩 없음), 섹션 없음, embed 작업 없음
    (chunk,) = await chunks_of(sm, empty)
    assert chunk.content.startswith("빈메모.txt")
    assert chunk.filename == "빈메모.txt"
    assert chunk.embedding is None
    assert [j.type for j in await jobs_of(sm, empty)] == [JobType.EXTRACT]
    async with sm() as s:
        assert await s.scalar(select(func.count()).select_from(Section)) == 0
    assert await runner.claim_embed() is None
    assert embedder.texts == []


async def test_unsupported_format_keeps_filename_chunk(
    sm: SM, tmp_path: Path, runner: JobRunner
) -> None:
    sheet = await upload(sm, tmp_path, "매출표.xlsx", b"PK\x03\x04 fake xlsx")
    await run_prep(runner)
    d = await doc(sm, sheet)
    assert d.status == DocumentStatus.FAILED
    assert d.failure_code == FailureCode.UNSUPPORTED_FORMAT
    (chunk,) = await chunks_of(sm, sheet)
    assert chunk.content.startswith("매출표.xlsx")
    assert list((tmp_path / "uploads").iterdir()) == []


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


async def test_index_replaces_chunks_atomically(
    sm: SM, tmp_path: Path, runner: JobRunner, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = await upload(sm, tmp_path, "계약.md", MD_THREE_SECTIONS)
    await runner.run_until_idle()
    before = await chunks_of(sm, doc_id)
    assert len(before) == 3
    assert (await doc(sm, doc_id)).status == DocumentStatus.READY

    new_text = MD_THREE_SECTIONS.replace("3월 둘째 주", "4월 첫째 주").replace(
        "1,200만 원", "1,500만 원"
    )
    await upload(sm, tmp_path, "계약.md", new_text)
    extract = await runner.claim(PREP_TYPES)
    assert extract is not None and extract.type == JobType.EXTRACT
    assert await runner.run(extract) == "completed"

    # 청크 교체 도중(이전 청크를 지운 뒤) 실패하면 트랜잭션이 통째로 되돌려진다
    from app.jobs import runner as runner_module

    real_chunk_row = runner_module.chunk_row
    calls = {"n": 0}

    def failing_chunk_row(*args: Any, **kwargs: Any) -> Chunk:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("insert failed")
        return real_chunk_row(*args, **kwargs)

    monkeypatch.setattr(runner_module, "chunk_row", failing_chunk_row)
    index = await runner.claim(PREP_TYPES)
    assert index is not None and index.type == JobType.INDEX
    assert await runner.run(index) == "released"
    after_failure = await chunks_of(sm, doc_id)
    assert [(c.id, c.content) for c in after_failure] == [(c.id, c.content) for c in before]
    assert all(c.embedding is not None for c in after_failure)

    # 다시 돌면 새 청크로 한 번에 바뀐다(seq 0부터 연속, 이전 내용 없음)
    monkeypatch.setattr(runner_module, "chunk_row", real_chunk_row)
    clock.advance(minutes=2)
    await runner.run_until_idle()
    after = await chunks_of(sm, doc_id)
    assert [c.seq for c in after] == [0, 1, 2]
    contents = "\n".join(c.content for c in after)
    assert "4월 첫째 주" in contents and "1,500만 원" in contents
    assert "3월 둘째 주" not in contents
    assert {c.id for c in after}.isdisjoint({c.id for c in before})
    assert (await doc(sm, doc_id)).status == DocumentStatus.READY


# ---------------------------------------------------------------------------
# embed와 하루 한도
# ---------------------------------------------------------------------------


async def test_quota_charged_once_per_version(
    sm: SM, tmp_path: Path, runner: JobRunner, clock: Clock, embedder: CountingEmbedder
) -> None:
    doc_id = await upload(sm, tmp_path, "회의록.md", MD_THREE_SECTIONS)
    await run_prep(runner)
    d = await doc(sm, doc_id)
    assert d.status == DocumentStatus.INDEXED

    # 첫 시도: 차감(used 1) 후 일시 오류로 반납
    embedder.failures.append(ProviderUnavailable("서버 오류(HTTP 503)", status_code=503))
    job = await claim_embed_job(runner)
    assert job.charged is True
    assert (await doc(sm, doc_id)).embed_charged_sha256 == d.sha256
    assert await runner.run(job) == "released"
    q = await quota_row(sm, clock)
    assert q is not None and q.used == 1

    # 같은 버전을 다시 가져가면 세지 않는다
    clock.advance(minutes=2)
    job = await claim_embed_job(runner)
    assert job.charged is False
    assert await runner.run(job) == "completed"
    q = await quota_row(sm, clock)
    assert q is not None and q.used == 1
    assert (await doc(sm, doc_id)).status == DocumentStatus.READY

    # 새 버전(내용이 바뀜)은 다시 1개로 센다
    await upload(sm, tmp_path, "회의록.md", MD_THREE_SECTIONS + "\n# 결론\n\n다음 회의는 금요일.\n")
    await run_prep(runner)
    job = await claim_embed_job(runner)
    assert job.charged is True
    await runner.run(job)
    q = await quota_row(sm, clock)
    assert q is not None and q.used == 2


async def test_quota_exhausted_waits_until_kst_midnight(
    sm: SM, tmp_path: Path, runner: JobRunner, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_env(monkeypatch, EMBED_DAILY_FILE_LIMIT="1")
    newer = await upload(sm, tmp_path, "new.md", "# 새 문서\n\n최근 수정한 문서.", mtime=clock())
    older = await upload(
        sm, tmp_path, "old.md", "# 옛 문서\n\n오래된 문서.", mtime=clock() - timedelta(days=3)
    )
    await run_prep(runner)

    # 최근 수정순으로 하나만 처리된다
    first = await claim_embed_job(runner)
    assert first.document_id == newer
    await runner.run(first)

    with capture_logs() as logs:
        exhausted = await runner.claim_embed()
        again = await runner.claim_embed()
    assert isinstance(exhausted, QuotaExhausted) and isinstance(again, QuotaExhausted)
    assert exhausted.reason == "files" and exhausted.used == 1 and exhausted.limit == 1
    local = exhausted.resume_at.astimezone(KST)
    assert (local.hour, local.minute, local.second) == (0, 0, 0)
    assert local.date() == clock().astimezone(KST).date() + timedelta(days=1)
    (logged,) = events(logs, "quota.exhausted")  # 같은 날·같은 이유는 한 번만
    assert logged["reason"] == "files" and logged["resume_at"] == exhausted.resume_at.isoformat()
    assert (await doc(sm, older)).status == DocumentStatus.INDEXED

    # 한도에 걸려도 extract·index는 계속 처리한다
    third = await upload(sm, tmp_path, "third.txt", "세 번째 문서", mtime=clock())
    assert await run_prep(runner) == 2
    assert (await doc(sm, third)).status == DocumentStatus.INDEXED
    summary = await runner.run_until_idle()
    assert summary.quota_exhausted is not None and summary.total == 0

    # 자정 1분 전에도 그대로, 자정(한국 시간)이 지나면 새 날짜 키로 다시 처리한다
    clock.now = exhausted.resume_at - timedelta(minutes=1)
    assert isinstance(await runner.claim_embed(), QuotaExhausted)
    clock.now = exhausted.resume_at + timedelta(seconds=1)
    job = await claim_embed_job(runner)
    assert job.charged is True
    q = await quota_row(sm, clock)
    assert q is not None and q.used == 1 and q.day == local.date()


async def test_unchanged_chunks_reuse_embeddings_without_quota(
    sm: SM, tmp_path: Path, runner: JobRunner, clock: Clock, embedder: CountingEmbedder
) -> None:
    doc_id = await upload(sm, tmp_path, "노트.md", MD_THREE_SECTIONS)
    await runner.run_until_idle()
    assert len(embedder.texts) == 3
    original = {c.content_sha256: c.embedding for c in await chunks_of(sm, doc_id)}
    q = await quota_row(sm, clock)
    assert q is not None and q.used == 1 and q.chunks_used == 3

    # 파일은 바뀌었지만(끝 공백) 청크 내용은 같음 → 벡터 재사용, API·한도 사용 없음, 바로 ready
    with capture_logs() as logs:
        await upload(sm, tmp_path, "노트.md", MD_THREE_SECTIONS + "\n\n\n")
        await runner.run_until_idle()
    d = await doc(sm, doc_id)
    assert d.status == DocumentStatus.READY
    assert d.embed_charged_sha256 != d.sha256  # 이 버전은 차감하지 않았다
    assert len(embedder.texts) == 3
    q = await quota_row(sm, clock)
    assert q is not None and q.used == 1 and q.chunks_used == 3
    jobs = await jobs_of(sm, doc_id)
    assert not any(j.type == JobType.EMBED and j.sha256 == d.sha256 for j in jobs)
    (index_done,) = [e for e in events(logs, "job.completed") if e["type"] == "index"]
    assert index_done["reused"] == 3 and index_done["to_embed"] == 0
    for c in await chunks_of(sm, doc_id):
        assert c.embedding is not None
        assert list(c.embedding) == list(original[c.content_sha256])

    # 일부만 바뀌면 바뀐 청크만 임베딩한다(한도는 버전당 1개)
    await upload(sm, tmp_path, "노트.md", MD_THREE_SECTIONS + "\n# 추가\n\n새로 쓴 문단.\n")
    await runner.run_until_idle()
    assert len(embedder.texts) == 4
    assert "새로 쓴 문단." in embedder.texts[-1]
    q = await quota_row(sm, clock)
    assert q is not None and q.used == 2 and q.chunks_used == 4
    assert (await doc(sm, doc_id)).status == DocumentStatus.READY


async def test_stale_version_result_discarded(
    sm: SM, tmp_path: Path, runner: JobRunner, embedder: CountingEmbedder
) -> None:
    # 1) extract 도중 새 버전이 올라오면 섹션을 저장하지 않고 버린다
    doc_id = await upload(sm, tmp_path, "보고서.md", "# 1판\n\n처음 버전.")
    job = await runner.claim(PREP_TYPES)
    assert job is not None
    await upload(sm, tmp_path, "보고서.md", "# 2판\n\n고친 버전.")
    with capture_logs() as logs:
        assert await runner.run(job) == "discarded"
    (discarded,) = events(logs, "job.discarded_stale")
    assert discarded["job_id"] == job.id and discarded["stage"] == "save"
    assert not events(logs, "job.completed")
    async with sm() as s:
        assert await s.scalar(select(func.count()).select_from(Section)) == 0
    assert (await jobs_of(sm, doc_id))[0].status == JobStatus.DONE

    # 새 버전은 정상 처리된다
    await runner.run_until_idle()
    d = await doc(sm, doc_id)
    assert d.status == DocumentStatus.READY
    assert "고친 버전." in (await chunks_of(sm, doc_id))[0].content

    # 2) 임베딩 API를 부르는 사이에 새 버전이 올라오면 벡터를 저장하지 않는다
    async def new_version_arrives() -> None:
        embedder.before = None
        await upload(sm, tmp_path, "보고서.md", "# 3판\n\n또 고친 버전.")

    await upload(sm, tmp_path, "보고서.md", "# 2판\n\n고친 버전. 조금 더.")
    await run_prep(runner)
    embed_job = await claim_embed_job(runner)
    embedder.before = new_version_arrives
    with capture_logs() as logs:
        assert await runner.run(embed_job) == "discarded"
    assert events(logs, "job.discarded_stale")[0]["type"] == "embed"
    chunks = await chunks_of(sm, doc_id)
    assert all(c.embedding is None for c in chunks if "조금 더" in c.content)
    assert (await doc(sm, doc_id)).status == DocumentStatus.QUEUED

    await runner.run_until_idle()
    d = await doc(sm, doc_id)
    assert d.status == DocumentStatus.READY
    assert "또 고친 버전." in (await chunks_of(sm, doc_id))[0].content

    # 3) 대기 중인 지난 버전의 embed 작업은 가져갈 때 버리고, 한도를 차감하지 않는다
    await upload(sm, tmp_path, "보고서.md", "# 4판\n\n네 번째 버전.")
    await run_prep(runner)
    q_before = await quota_row(sm, runner.clock)
    await upload(sm, tmp_path, "보고서.md", "# 5판\n\n다섯 번째 버전.")
    with capture_logs() as logs:
        assert await runner.claim_embed() is None
    (discarded,) = events(logs, "job.discarded_stale")
    assert discarded["stage"] == "claim" and discarded["type"] == "embed"
    q_after = await quota_row(sm, runner.clock)
    assert q_before is not None and q_after is not None and q_after.used == q_before.used


async def test_daily_chunk_limit_guard(
    sm: SM, tmp_path: Path, runner: JobRunner, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_env(monkeypatch, EMBED_DAILY_CHUNK_LIMIT="2")
    big = await upload(sm, tmp_path, "big.md", MD_THREE_SECTIONS, mtime=clock())
    small = await upload(
        sm, tmp_path, "small.md", "# 짧은 글\n\n한 문단.", mtime=clock() - timedelta(days=1)
    )
    await run_prep(runner)

    # 진행 중인 파일은 상한을 넘어도 끝까지 처리한다
    job = await claim_embed_job(runner)
    assert job.document_id == big
    assert await runner.run(job) == "completed"
    q = await quota_row(sm, clock)
    assert q is not None and q.chunks_used == 3 and q.used == 1

    # 상한을 넘었으면 파일 한도(100)가 남아도 다음 날까지 쉰다
    with capture_logs() as logs:
        blocked = await runner.claim_embed()
    assert isinstance(blocked, QuotaExhausted)
    assert blocked.reason == "chunks" and blocked.used == 3 and blocked.limit == 2
    assert events(logs, "quota.exhausted")[0]["reason"] == "chunks"
    assert (await doc(sm, small)).status == DocumentStatus.INDEXED

    # 0이면 제한 없음
    set_env(monkeypatch, EMBED_DAILY_CHUNK_LIMIT="0")
    job = await claim_embed_job(runner)
    assert job.document_id == small


async def test_rate_limited_job_requeued_without_recharge(
    sm: SM,
    tmp_path: Path,
    runner: JobRunner,
    clock: Clock,
    embedder: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_env(monkeypatch, JOB_MAX_ATTEMPTS="3")
    runner.settings = get_settings()
    doc_id = await upload(sm, tmp_path, "요약.md", MD_THREE_SECTIONS)
    await run_prep(runner)

    # 429가 계속되면 30분 뒤로 미뤄 반납하고, 문서는 indexed(BM25 검색은 계속됨)
    embedder.failures.append(RateLimited("호출 한도 초과(HTTP 429)", status_code=429))
    job = await claim_embed_job(runner)
    assert job.charged is True
    with capture_logs() as logs:
        assert await runner.run(job) == "released"
    (failed,) = events(logs, "job.failed")
    assert failed["error"] == "RateLimited" and failed["status_code"] == 429
    assert failed["final"] is False
    (embed_row,) = [j for j in await jobs_of(sm, doc_id) if j.type == JobType.EMBED]
    assert embed_row.status == JobStatus.QUEUED
    assert embed_row.run_after == clock() + timedelta(minutes=30)
    assert (await doc(sm, doc_id)).status == DocumentStatus.INDEXED

    # 30분이 지나기 전에는 가져가지 않는다
    clock.advance(minutes=29)
    assert await runner.claim_embed() is None
    # 지나면 다시 가져가되 같은 버전이라 다시 차감하지 않는다
    clock.advance(minutes=2)
    embedder.failures.append(RateLimited("호출 한도 초과(HTTP 429)", status_code=429))
    job = await claim_embed_job(runner)
    assert job.charged is False and job.attempts == 2
    assert await runner.run(job) == "released"
    q = await quota_row(sm, clock)
    assert q is not None and q.used == 1

    # 재시도 상한(JOB_MAX_ATTEMPTS)을 넘으면 failed
    clock.advance(minutes=31)
    embedder.failures.append(RateLimited("호출 한도 초과(HTTP 429)", status_code=429))
    job = await claim_embed_job(runner)
    assert job.charged is False and job.attempts == 3
    with capture_logs() as logs:
        assert await runner.run(job) == "failed"
    assert events(logs, "job.failed")[0]["final"] is True
    (embed_row,) = [j for j in await jobs_of(sm, doc_id) if j.type == JobType.EMBED]
    assert embed_row.status == JobStatus.FAILED
    d = await doc(sm, doc_id)
    assert d.status == DocumentStatus.FAILED
    q = await quota_row(sm, clock)
    assert q is not None and q.used == 1
    # 청크와 BM25 텍스트는 남아 있다
    assert len(await chunks_of(sm, doc_id)) == 3


async def test_worker_heartbeat_and_loop_process_jobs(
    sm: SM, tmp_path: Path, runner: JobRunner
) -> None:
    """Worker 프로세스: 시작하면 하트비트를 남기고, 두 루프가 큐를 비운다."""
    from app.db.models import WorkerHeartbeat
    from app.jobs.worker import Worker

    doc_id = await upload(sm, tmp_path, "worker.md", MD_THREE_SECTIONS)
    worker = Worker(runner, sm, name="test-worker")
    worker.poll_seconds = 0.05

    async def stop_when_ready() -> None:
        for _ in range(200):
            if (await doc(sm, doc_id)).status == DocumentStatus.READY:
                break
            await asyncio.sleep(0.05)
        worker.stop()

    with capture_logs() as logs:
        await asyncio.wait_for(asyncio.gather(worker.run(), stop_when_ready()), timeout=30)
    assert (await doc(sm, doc_id)).status == DocumentStatus.READY
    assert [e["type"] for e in events(logs, "job.completed")] == ["extract", "index", "embed"]
    assert events(logs, "worker.started") and events(logs, "worker.stopped")
    async with sm() as s:
        hb = await s.get(WorkerHeartbeat, "test-worker")
    assert hb is not None and hb.pid is not None
