"""Ingestion Worker 프로세스 (TRD 3.2, 6.2, 12.4, 12.5). 실행: `rag-backend worker`.

한 프로세스에서 작업 루프 두 개와 APScheduler를 함께 돌린다.

- 준비 루프: `extract`·`index`를 하나씩 처리(파싱 동시성 1). 한도와 상관없이 계속 처리한다.
- 임베딩 루프: `embed`를 하루 한도 안에서 처리(API 호출 동시 2개는 `JobRunner`가 지킴).
  한도에 닿으면 다음 날 0시(한국 시간)까지 쉰다. 그 사이에도 1분마다 이미 차감한 버전
  (429로 반납된 작업)이 있는지 확인한다.
- APScheduler: 30초마다 하트비트(`worker_heartbeats`), 1분마다 멈춘 작업 재대기와
  하트비트가 끊긴 동기화 실행의 `abandoned` 처리(`SYNC_RUN_STALE_MINUTES`),
  매일 0시(한국 시간)에 임베딩 루프를 깨운다. 한도는 날짜 키가 바뀌어 자연히 초기화된다.
"""

import asyncio
import os
from collections.abc import Awaitable
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models import WorkerHeartbeat
from app.jobs.quota import QuotaExhausted, quota_tz
from app.jobs.runner import PREP_TYPES, JobRunner
from app.sync.runs import abandon_stale_runs

log = get_logger(__name__)

HEARTBEAT_SECONDS = 30
STALE_CHECK_SECONDS = 60
# 한도에 걸려 쉬는 동안 이미 차감한 버전이 생겼는지 확인하는 간격
EXHAUSTED_RECHECK_SECONDS = 60.0


class Worker:
    def __init__(
        self,
        runner: JobRunner,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        name: str = "worker",
    ) -> None:
        self.runner = runner
        self.sessionmaker = sessionmaker
        self.name = name
        self.poll_seconds = runner.settings.worker_poll_seconds
        self._stop = asyncio.Event()
        self._prep_wake = asyncio.Event()
        self._embed_wake = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        self.wake()

    def wake(self) -> None:
        self._prep_wake.set()
        self._embed_wake.set()

    async def heartbeat(self) -> None:
        now = self.runner.clock()
        stmt = insert(WorkerHeartbeat).values(
            name=self.name, started_at=now, heartbeat_at=now, pid=os.getpid()
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[WorkerHeartbeat.name],
            set_={"heartbeat_at": now, "pid": os.getpid()},
        )
        async with self.sessionmaker() as s, s.begin():
            await s.execute(stmt)

    async def _register(self) -> None:
        """시작 시 하트비트 행의 시작 시각을 새로 기록한다."""
        now = self.runner.clock()
        stmt = insert(WorkerHeartbeat).values(
            name=self.name, started_at=now, heartbeat_at=now, pid=os.getpid()
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[WorkerHeartbeat.name],
            set_={"started_at": now, "heartbeat_at": now, "pid": os.getpid()},
        )
        async with self.sessionmaker() as s, s.begin():
            await s.execute(stmt)

    async def _safe(self, name: str, coro: Awaitable[object]) -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001 - 예약 작업 실패가 Worker를 멈추지 않게
            log.warning("worker.scheduled_failed", job=name, error=type(e).__name__)

    async def _heartbeat_job(self) -> None:
        await self._safe("heartbeat", self.heartbeat())

    async def _stale_job(self) -> None:
        async def run() -> None:
            if await self.runner.requeue_stale():
                self.wake()

        await self._safe("requeue_stale", run())

    async def abandon_stale_sync_runs(self) -> list[int]:
        stale_after = timedelta(minutes=self.runner.settings.sync_run_stale_minutes)
        async with self.sessionmaker() as s, s.begin():
            return await abandon_stale_runs(s, now=self.runner.clock(), stale_after=stale_after)

    async def _sync_runs_job(self) -> None:
        await self._safe("abandon_stale_sync_runs", self.abandon_stale_sync_runs())

    async def _midnight_job(self) -> None:
        log.info("quota.day_started")
        self._embed_wake.set()

    async def _wait(self, event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, seconds))
        except TimeoutError:
            pass
        event.clear()

    async def _prep_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = await self.runner.run_next(PREP_TYPES)
            except Exception as e:  # noqa: BLE001 - DB 연결 끊김 등. 잠시 뒤 다시 시도
                log.warning("worker.loop_error", loop="prep", error=type(e).__name__)
                job = None
            if job is None:
                await self._wait(self._prep_wake, self.poll_seconds)
            else:
                self._embed_wake.set()  # index가 embed 작업을 등록했을 수 있다

    async def _embed_loop(self) -> None:
        while not self._stop.is_set():
            try:
                claimed = await self.runner.claim_embed()
            except Exception as e:  # noqa: BLE001
                log.warning("worker.loop_error", loop="embed", error=type(e).__name__)
                claimed = None
            if isinstance(claimed, QuotaExhausted):
                until_reset = (claimed.resume_at - self.runner.clock()).total_seconds()
                await self._wait(self._embed_wake, min(until_reset, EXHAUSTED_RECHECK_SECONDS))
            elif claimed is None:
                await self._wait(self._embed_wake, self.poll_seconds)
            else:
                await self.runner.run(claimed)

    def build_scheduler(self) -> AsyncIOScheduler:
        """예약 작업을 등록한 스케줄러(시작 전). 작업 ID로 테스트가 등록 여부를 확인한다."""
        scheduler = AsyncIOScheduler(timezone=quota_tz())
        scheduler.add_job(
            self._heartbeat_job, "interval", seconds=HEARTBEAT_SECONDS, id="heartbeat"
        )
        scheduler.add_job(
            self._stale_job, "interval", seconds=STALE_CHECK_SECONDS, id="requeue_stale"
        )
        scheduler.add_job(
            self._sync_runs_job,
            "interval",
            seconds=STALE_CHECK_SECONDS,
            id="abandon_stale_sync_runs",
        )
        scheduler.add_job(self._midnight_job, "cron", hour=0, minute=0, id="quota_midnight")
        return scheduler

    async def run(self) -> None:
        """`stop()`이 불릴 때까지 돈다."""
        await self._register()
        scheduler = self.build_scheduler()
        scheduler.start()
        settings = self.runner.settings
        log.info(
            "worker.started",
            name=self.name,
            embed_model=self.runner.embedder.model,
            daily_file_limit=settings.embed_daily_file_limit,
            daily_chunk_limit=settings.embed_daily_chunk_limit,
            stale_after=str(timedelta(minutes=settings.job_stale_lock_minutes)),
        )
        try:
            await self.runner.requeue_stale()
            await self._sync_runs_job()
            await asyncio.gather(self._prep_loop(), self._embed_loop())
        finally:
            scheduler.shutdown(wait=False)
            log.info("worker.stopped", name=self.name)
