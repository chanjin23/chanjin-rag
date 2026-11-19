"""동기화 흐름 (TRD 6.1 시퀀스, Agent 쪽 규칙).

가짜 Sync API 서버(httpx.MockTransport)에 실행한다.
"""

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from fakes import TOKEN, FakeLock, FakeSyncServer, write

from rag_agent import logs
from rag_agent.lock import NamedMutexLock
from rag_agent.state import StateDB
from rag_agent.sync import SyncRunner


def _runner(server: FakeSyncServer, state: StateDB, lock: Any = None, **kw: Any) -> SyncRunner:
    return SyncRunner(server.client(), state, lock or FakeLock(), **kw)


def _names(paths: list[str]) -> set[str]:
    return {p.replace("\\", "/").rsplit("/", 1)[-1] for p in paths}


def test_full_sync_against_fake_server(root: Path, state: StateDB) -> None:
    write(root / "notes" / "a.md", "# 제목\n본문")
    write(root / "notes" / "b.TXT", "텍스트")
    write(root / "big.pdf", b"%PDF" + b"0" * (1024 * 1024 + 10))  # max_file_mb=1 초과 → meta_only
    write(root / "vanish.md", "판정 뒤 사라질 파일")
    write(root / "old.doc", b"x")
    write(root / "project" / "skip.md")
    server = FakeSyncServer(root, max_file_mb=1)
    server.on_manifest = lambda body: (root / "vanish.md").unlink()
    lock = FakeLock()

    outcome = _runner(server, state, lock).run("cli")

    assert outcome.status == "succeeded" and outcome.run_id == 1
    # 순서: config → runs → manifest → 업로드 → meta → complete
    kinds = [f"{m} {p}" for m, p in server.calls if not p.endswith("/heartbeat")]
    assert kinds[:3] == ["GET /config", "POST /runs", "POST /runs/1/manifest"]
    assert kinds[3:5] == ["PUT /files", "PUT /files"]
    assert kinds[5:] == ["POST /files/meta", "POST /runs/1/complete"]
    assert lock.acquired == 1 and lock.released == 1

    manifest = server.manifests[0]
    assert _names([f["path"] for f in manifest["files"]]) == {
        "a.md",
        "b.TXT",
        "big.pdf",
        "vanish.md",
    }
    assert manifest["scan_errors"] == []
    assert _names([u["path"] for u in server.uploads]) == {"a.md", "b.TXT"}
    assert all(u["run_id"] == "1" for u in server.uploads)
    assert _names([f["path"] for f in server.meta[0]["files"]]) == {"big.pdf"}
    assert server.meta[0]["run_id"] == 1

    stats = server.completes[0]["stats"]
    assert server.completes[0]["status"] == "succeeded"
    assert stats["uploaded"] == 2
    assert stats["meta_registered"] == 1
    assert stats["vanished"] == 1  # 판정 뒤 사라진 파일은 건너뜀
    assert stats["upload_failed"] == 0
    assert stats["skipped_formats"] == {".doc": 1}
    assert stats["retry_queue"] == 0
    assert state.get("last_run")["status"] == "succeeded"
    assert state.get("config")["allowed_root"] == str(root)

    # 두 번째 실행: 바뀐 것이 없으므로 해시를 다시 계산하지 않고 업로드도 없다
    server.on_manifest = lambda body: None
    again = _runner(server, state).run("schedule")
    assert again.status == "succeeded" and again.run_id == 2
    assert again.stats["hashed"] == 0 and again.stats["hash_reused"] == 3
    assert again.stats["uploaded"] == 0 and len(server.uploads) == 2


def test_upload_failure_goes_to_retry_queue(root: Path, state: StateDB) -> None:
    write(root / "ok.md", "정상")
    write(root / "flaky.md", "서버 오류가 날 파일")
    server = FakeSyncServer(root)
    server.fail_upload = lambda path: path.endswith("flaky.md")

    first = _runner(server, state).run("cli")

    # 일부 업로드 실패는 실행 실패가 아니다(다음 실행에서 재시도)
    assert first.status == "succeeded"
    assert first.stats["uploaded"] == 1 and first.stats["upload_failed"] == 1
    queue = state.retry_items()
    assert _names([item.path for item in queue.values()]) == {"flaky.md"}
    assert next(iter(queue.values())).reason == "internal_error"
    assert first.stats["retry_queue"] == 1

    # 다음 실행도 실패하면 시도 횟수만 늘어난다
    second = _runner(server, state).run("cli")
    assert second.stats["upload_failed"] == 1
    assert next(iter(state.retry_items().values())).attempts == 2

    # 서버가 회복되면 manifest가 다시 upload로 판정하고, 성공하면 큐에서 지운다
    server.fail_upload = lambda path: False
    third = _runner(server, state).run("cli")
    assert third.stats["uploaded"] == 1 and third.stats["upload_failed"] == 0
    assert state.retry_items() == {}
    assert _names([u["path"] for u in server.uploads]) == {"ok.md", "flaky.md"}


def test_single_instance_exits(root: Path, state: StateDB) -> None:
    write(root / "a.md")
    server = FakeSyncServer(root)

    outcome = _runner(server, state, FakeLock(held_by_other=True)).run("web")

    assert outcome.status == "already_running"
    assert server.calls == []  # 서버에 아무 요청도 하지 않고 바로 끝낸다

    if sys.platform == "win32":
        # 실제 이름 있는 뮤텍스: 같은 이름을 두 번째로 잡으면 실패하고, 놓으면 다시 잡힌다
        name = f"Local\\chanjin-rag-agent-test-{time.time_ns()}"
        first, second = NamedMutexLock(name), NamedMutexLock(name)
        assert first.acquire()
        try:
            assert not second.acquire()
            blocked = _runner(server, state, second).run("cli")
            assert blocked.status == "already_running" and server.calls == []
        finally:
            first.release()
        assert second.acquire()
        second.release()


def test_heartbeat_sent_while_running(root: Path, state: StateDB) -> None:
    write(root / "a.md")
    server = FakeSyncServer(root)
    server.on_manifest = lambda body: time.sleep(0.3)

    outcome = _runner(server, state, heartbeat_s=0.05).run("cli")

    assert outcome.status == "succeeded"
    assert server.heartbeats >= 1
    beats = [i for i, (_, p) in enumerate(server.calls) if p.endswith("/heartbeat")]
    complete = next(i for i, (_, p) in enumerate(server.calls) if p.endswith("/complete"))
    assert beats and max(beats) < complete  # 완료 뒤에는 보내지 않는다


def test_revoked_token_fails_run_and_completes_failed(root: Path, state: StateDB) -> None:
    write(root / "a.md")
    server = FakeSyncServer(root)

    outcome = SyncRunner(server.client(token="revoked"), state, FakeLock()).run("cli")

    assert outcome.status == "failed" and outcome.error == "invalid_token"
    assert state.get("last_run")["error"] == "invalid_token"


def test_agent_log_has_events_without_token(tmp_path: Path, root: Path, state: StateDB) -> None:
    write(root / "a.md", "본문은 로그에 남지 않는다")
    server = FakeSyncServer(root)
    log_file = tmp_path / "home" / "agent.log"
    logger = logs.configure(log_file, secrets=[TOKEN])
    try:
        _runner(server, state, logger=logger).run("cli")
        logs.event(logger, "agent.test.secret", note=f"token={TOKEN}")  # 혹시 섞여도 가린다
    finally:
        logs.close()

    text = log_file.read_text(encoding="utf-8")
    events = [json.loads(line)["event"] for line in text.splitlines()]
    assert events[:4] == [
        "agent.sync.started",
        "agent.scan.completed",
        "agent.manifest.judged",
        "agent.sync.completed",
    ]
    assert TOKEN not in text
    assert "본문은" not in text and "a.md" not in text
    handler = logging.getLogger(logs.LOGGER_NAME)
    assert handler.handlers == []


def test_log_rotation_keeps_seven_days(tmp_path: Path) -> None:
    from logging.handlers import TimedRotatingFileHandler

    logger = logs.configure(tmp_path / "agent.log")
    try:
        (handler,) = logger.handlers
        assert isinstance(handler, TimedRotatingFileHandler)
        assert handler.when == "MIDNIGHT" and handler.backupCount == 7
    finally:
        logs.close()


def test_lock_released_on_failure(root: Path, state: StateDB) -> None:
    server = FakeSyncServer(root)
    server.config = {}  # 설정 응답이 이상해 실패로 끝나도 잠금은 푼다
    lock = FakeLock()
    outcome = _runner(server, state, lock).run("cli")
    assert outcome.status == "failed"
    assert lock.released == 1
