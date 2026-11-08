"""동기화 실행 기록 `sync_runs` (TRD 5.1, 6.1 Agent 쪽 규칙, 8.2).

시작 → 하트비트 → 완료(통계 저장), 하트비트가 10분(`SYNC_RUN_STALE_MINUTES`) 넘게 끊긴 실행의
`abandoned` 처리(Worker 예약 작업).
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.db.enums import SyncRunSource, SyncRunStatus
from app.db.models import EMBED_DIM, SyncRun
from app.jobs.runner import JobRunner
from app.jobs.worker import Worker
from app.providers.fake import FakeEmbeddingProvider
from app.sync.runs import abandon_stale_runs

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


async def test_stale_run_abandoned(session: AsyncSession) -> None:
    def run(minutes_ago: int, status: SyncRunStatus = SyncRunStatus.RUNNING) -> SyncRun:
        r = SyncRun(
            source=SyncRunSource.SCHEDULE,
            started_at=NOW - timedelta(hours=1),
            heartbeat_at=NOW - timedelta(minutes=minutes_ago),
            status=status,
        )
        session.add(r)
        return r

    stale = run(11)
    fresh = run(9)
    finished = run(30, SyncRunStatus.SUCCEEDED)
    await session.flush()

    with capture_logs() as logs:
        ids = await abandon_stale_runs(session, now=NOW, stale_after=timedelta(minutes=10))

    assert ids == [stale.id]
    for r in (stale, fresh, finished):
        await session.refresh(r)
    assert stale.status == SyncRunStatus.ABANDONED
    assert stale.finished_at == NOW
    assert fresh.status == SyncRunStatus.RUNNING
    assert finished.status == SyncRunStatus.SUCCEEDED
    assert [e["count"] for e in logs if e["event"] == "sync.run.abandoned"] == [1]

    # 다시 돌려도 바뀌는 것이 없다
    assert await abandon_stale_runs(session, now=NOW, stale_after=timedelta(minutes=10)) == []


async def test_run_lifecycle(sync_client: httpx.AsyncClient, session: AsyncSession) -> None:
    with capture_logs() as logs:
        res = await sync_client.post("/api/sync/runs", json={"source": "web"})
        assert res.status_code == 201
        run_id = res.json()["run_id"]
        assert res.json()["status"] == "running"

        res = await sync_client.post(f"/api/sync/runs/{run_id}/manifest", json={"files": []})
        assert res.status_code == 200
        res = await sync_client.post(f"/api/sync/runs/{run_id}/heartbeat")
        assert res.status_code == 200
        res = await sync_client.post(
            f"/api/sync/runs/{run_id}/complete",
            json={
                "status": "succeeded",
                "stats": {
                    "uploaded": 3,
                    "upload_failed": 1,
                    "skipped_formats": {".doc": 2, ".xls": 1},
                    "manifest": "덮어쓰기 시도",
                },
            },
        )
        assert res.status_code == 200
        assert res.json()["status"] == "succeeded"

    run = await session.get(SyncRun, run_id)
    assert run is not None
    await session.refresh(run)
    assert run.source == SyncRunSource.WEB
    assert run.finished_at is not None
    assert run.stats["uploaded"] == 3
    assert run.stats["skipped_formats"] == {".doc": 2, ".xls": 1}
    # 서버가 판정 때 남긴 통계는 Agent 값으로 덮이지 않는다
    assert isinstance(run.stats["manifest"], dict)
    events = [e["event"] for e in logs if e["event"].startswith("sync.run.")]
    assert events == ["sync.run.started", "sync.run.completed"]

    # 끝난 실행에는 하트비트·manifest·완료를 받지 않는다
    for url in ("heartbeat", "complete"):
        res = await sync_client.post(f"/api/sync/runs/{run_id}/{url}", json={})
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "run_finished"
    res = await sync_client.post("/api/sync/runs/999999/heartbeat")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "run_not_found"


async def test_heartbeat_resumes_abandoned_run(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    res = await sync_client.post("/api/sync/runs", json={"source": "schedule"})
    run_id = res.json()["run_id"]
    run = await session.get(SyncRun, run_id)
    assert run is not None
    run.heartbeat_at = datetime.now(UTC) - timedelta(minutes=30)
    await session.flush()
    assert await abandon_stale_runs(
        session, now=datetime.now(UTC), stale_after=timedelta(minutes=10)
    ) == [run_id]

    # PC가 절전에서 깨어나 하트비트를 보내면 다시 진행 중
    res = await sync_client.post(f"/api/sync/runs/{run_id}/heartbeat")
    assert res.status_code == 200
    assert res.json()["status"] == "running"
    assert res.json()["finished_at"] is None


async def test_worker_schedules_abandon_stale_sync_runs(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker의 APScheduler가 1분마다 하트비트 끊긴 실행을 정리하고, 설정값(분)을 따른다."""
    monkeypatch.setenv("SYNC_RUN_STALE_MINUTES", "15")
    get_settings.cache_clear()
    # 테스트 세션과 같은 연결(같은 롤백 트랜잭션)을 쓰는 세션 팩토리. Worker는 시계가 고정
    sm = async_sessionmaker(
        bind=session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    worker = Worker(JobRunner(sm, FakeEmbeddingProvider(EMBED_DIM), clock=lambda: NOW), sm)

    jobs = {job.id: job for job in worker.build_scheduler().get_jobs()}
    job = jobs["abandon_stale_sync_runs"]
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(minutes=1)

    def run(minutes_ago: int) -> SyncRun:
        r = SyncRun(
            source=SyncRunSource.WEB,
            started_at=NOW - timedelta(hours=1),
            heartbeat_at=NOW - timedelta(minutes=minutes_ago),
        )
        session.add(r)
        return r

    stale, recent = run(16), run(12)  # 15분 기준: 16분 전만 정리
    await session.flush()

    with capture_logs() as logs:
        assert await worker.abandon_stale_sync_runs() == [stale.id]
    for r in (stale, recent):
        await session.refresh(r)
    assert stale.status == SyncRunStatus.ABANDONED and stale.finished_at == NOW
    assert recent.status == SyncRunStatus.RUNNING
    assert any(e["event"] == "sync.run.abandoned" for e in logs)
