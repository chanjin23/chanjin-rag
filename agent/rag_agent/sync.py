"""동기화 한 번 실행 (TRD 6.1 시퀀스, F-02·F-03).

config → runs → 스캔(해시 캐시) → manifest → 업로드 → 메타 등록 → complete → 백업 수신(TRD 10.3).
- 백업 수신은 `backups`(BackupFetcher)를 줄 때만 하고, 실패해도 동기화 결과는 바꾸지 않는다.
- 단일 인스턴스: 잠금을 얻지 못하면 서버에 아무 요청도 하지 않고 바로 끝낸다.
- 실행 중 1분마다 heartbeat(별도 스레드). 실패해도 동기화는 계속한다.
- 판정 뒤 업로드 전에 사라진 파일은 건너뛴다.
- 업로드·메타 등록 실패는 로컬 재시도 큐(`state.db`)에 남긴다. 다음 실행에서 manifest 판정을 다시
  받아 자연스럽게 재시도하고, 성공하거나 더 이상 업로드 판정이 나오지 않으면 큐에서 지운다.
- 로그에는 경로·파일명 없이 건수, `run_id`, 해시 앞 8자리만 남긴다.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from rag_agent.backup import BackupFetcher, BackupFetchResult
from rag_agent.client import ApiError, SyncClient
from rag_agent.keys import make_path_key
from rag_agent.lock import InstanceLock
from rag_agent.logs import event, get_logger
from rag_agent.rules import SyncRules
from rag_agent.scan import Opener, ScannedFile, Scanner, open_binary
from rag_agent.state import StateDB

HEARTBEAT_SECONDS = 60.0
META_BATCH = 500

Status = Literal["succeeded", "failed", "already_running"]


@dataclass
class SyncOutcome:
    status: Status
    run_id: int | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    backups: BackupFetchResult | None = None


class Heartbeat:
    """`interval`초마다 `POST /runs/{id}/heartbeat`. 실패는 로그만 남긴다."""

    def __init__(
        self, client: SyncClient, run_id: int, interval: float, logger: logging.Logger
    ) -> None:
        self._client = client
        self._run_id = run_id
        self._interval = interval
        self._log = logger
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="rag-agent-heartbeat", daemon=True)
        self.sent = 0

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._client.heartbeat(self._run_id)
                self.sent += 1
            except (ApiError, httpx.HTTPError) as e:
                event(
                    self._log,
                    "agent.heartbeat.failed",
                    logging.WARNING,
                    run_id=self._run_id,
                    error=_error_code(e),
                )


def _error_code(e: BaseException) -> str:
    if isinstance(e, ApiError):
        return e.code
    return type(e).__name__


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


@dataclass
class _Counters:
    uploaded: int = 0
    upload_unchanged: int = 0
    upload_failed: int = 0
    meta_registered: int = 0
    meta_failed: int = 0
    vanished: int = 0
    unknown: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


class SyncRunner:
    def __init__(
        self,
        client: SyncClient,
        state: StateDB,
        lock: InstanceLock,
        *,
        opener: Opener = open_binary,
        heartbeat_s: float = HEARTBEAT_SECONDS,
        limit_roots: list[str] | None = None,
        clock: Callable[[], float] = time.time,
        logger: logging.Logger | None = None,
        backups: BackupFetcher | None = None,
    ) -> None:
        self.client = client
        self.backups = backups
        self.state = state
        self.lock = lock
        self.opener = opener
        self.heartbeat_s = heartbeat_s
        self.limit_roots = limit_roots
        self.clock = clock
        self.log = logger or get_logger()

    def run(self, source: str) -> SyncOutcome:
        if not self.lock.acquire():
            event(self.log, "agent.instance.already_running", source=source)
            return SyncOutcome(status="already_running")
        try:
            outcome = self._run(source)
        finally:
            self.lock.release()
        return outcome

    def _run(self, source: str) -> SyncOutcome:
        started = self.clock()
        try:
            config = self.client.get_config()
            rules = SyncRules.from_config(config)
            self.state.set("config", rules.to_config())
            run_id = self.client.start_run(source)
        except (ApiError, httpx.HTTPError, KeyError, ValueError) as e:
            error = _error_code(e)
            event(self.log, "agent.sync.failed", logging.ERROR, source=source, error=error)
            outcome = SyncOutcome(status="failed", error=error)
            self._save_last(outcome, source, started)
            return outcome

        event(self.log, "agent.sync.started", run_id=run_id, source=source)
        stats: dict[str, Any] = {}
        try:
            with Heartbeat(self.client, run_id, self.heartbeat_s, self.log):
                stats = self._sync(rules, run_id)
            self.client.complete(run_id, "succeeded", stats)
            outcome = SyncOutcome(status="succeeded", run_id=run_id, stats=stats)
            event(self.log, "agent.sync.completed", run_id=run_id, status="succeeded", **stats)
        except (ApiError, httpx.HTTPError, OSError, KeyError, ValueError) as e:
            error = _error_code(e)
            event(self.log, "agent.sync.failed", logging.ERROR, run_id=run_id, error=error)
            try:
                self.client.complete(run_id, "failed", stats, error=error)
            except (ApiError, httpx.HTTPError):
                pass
            outcome = SyncOutcome(status="failed", run_id=run_id, stats=stats, error=error)
        if outcome.status == "succeeded" and self.backups is not None:
            outcome.backups = self.backups.fetch(run_id)
        self._save_last(outcome, source, started)
        return outcome

    def _save_last(self, outcome: SyncOutcome, source: str, started: float) -> None:
        self.state.set(
            "last_run",
            {
                "run_id": outcome.run_id,
                "status": outcome.status,
                "source": source,
                "started_at": _iso(started),
                "finished_at": _iso(self.clock()),
                "error": outcome.error,
                "stats": outcome.stats,
            },
        )

    def _sync(self, rules: SyncRules, run_id: int) -> dict[str, Any]:
        scan = Scanner(rules, self.state, self.opener).scan(self.limit_roots)
        now = self.clock()
        self.state.put_files([f.cached() for f in scan.files], now)
        self.state.prune_files({f.path_key for f in scan.files}, scan.scan_errors)
        event(self.log, "agent.scan.completed", run_id=run_id, **scan.stats())

        judged = self.client.manifest(
            run_id, [f.manifest_entry() for f in scan.files], scan.scan_errors
        )
        counts = judged.get("counts", {})
        event(self.log, "agent.manifest.judged", run_id=run_id, **counts)

        by_key = {f.path_key: f for f in scan.files}
        c = _Counters()
        failed: set[str] = set()
        for path in judged.get("upload", []):
            self._upload(run_id, by_key.get(make_path_key(path)), c, failed)
        self._register_meta(run_id, judged.get("meta_only", []), by_key, c, failed)

        # 재시도 큐 정리: 이번에 실패한 것만 남긴다(성공·더 이상 업로드 판정 아님·사라짐은 지움)
        stale = [k for k in self.state.retry_items() if k not in failed]
        self.state.remove_retry(stale)
        stats: dict[str, Any] = scan.stats()
        stats.update(c.as_dict())
        stats["retry_queue"] = len(self.state.retry_items())
        return stats

    def _upload(
        self, run_id: int, item: ScannedFile | None, c: _Counters, failed: set[str]
    ) -> None:
        if item is None:  # manifest에 보내지 않은 경로(서버 응답 이상)
            c.unknown += 1
            return
        if not os.path.isfile(item.fs_path):
            c.vanished += 1
            event(self.log, "agent.file.vanished", run_id=run_id, sha256_prefix=item.sha256[:8])
            return
        try:
            result = self.client.upload(run_id, item)
        except FileNotFoundError:
            c.vanished += 1
            event(self.log, "agent.file.vanished", run_id=run_id, sha256_prefix=item.sha256[:8])
            return
        except ApiError as e:
            if e.status == 401:
                raise  # 토큰 폐기: 이번 실행을 실패로 끝낸다
            self._failed(run_id, item, e.code, c, failed)
            return
        except (httpx.HTTPError, OSError) as e:
            self._failed(run_id, item, type(e).__name__, c, failed)
            return
        if result.get("outcome") == "unchanged":
            c.upload_unchanged += 1
        else:
            c.uploaded += 1

    def _failed(
        self,
        run_id: int,
        item: ScannedFile,
        reason: str,
        c: _Counters,
        failed: set[str],
        kind: Literal["upload", "meta"] = "upload",
    ) -> None:
        if kind == "meta":
            c.meta_failed += 1
        else:
            c.upload_failed += 1
        failed.add(item.path_key)
        self.state.add_retry(item.path_key, item.path, reason, self.clock())
        event(
            self.log,
            "agent.upload.failed",
            logging.WARNING,
            run_id=run_id,
            kind=kind,
            reason=reason,
            sha256_prefix=item.sha256[:8],
        )

    def _register_meta(
        self,
        run_id: int,
        paths: list[str],
        by_key: dict[str, ScannedFile],
        c: _Counters,
        failed: set[str],
    ) -> None:
        items: list[ScannedFile] = []
        for path in paths:
            item = by_key.get(make_path_key(path))
            if item is None:
                c.unknown += 1
            elif not os.path.isfile(item.fs_path):
                c.vanished += 1
            else:
                items.append(item)
        for i in range(0, len(items), META_BATCH):
            batch = items[i : i + META_BATCH]
            try:
                result = self.client.register_meta(run_id, [f.manifest_entry() for f in batch])
            except (ApiError, httpx.HTTPError) as e:
                if isinstance(e, ApiError) and e.status == 401:
                    raise
                for item in batch:
                    self._failed(run_id, item, _error_code(e), c, failed, kind="meta")
                continue
            c.meta_registered += len(result.get("accepted", []))
