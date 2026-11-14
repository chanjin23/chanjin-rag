"""백업 수신 (TRD 10.3, 12.2): 동기화가 끝나면 없는 백업만 내려받고, SHA-256을 검증하고, 7개만 둔다.

가짜 Sync API 서버(httpx.MockTransport)의 백업 내용은 합성 바이트다.
"""

import hashlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import FakeLock, FakeSyncServer, write

from rag_agent import logs
from rag_agent.backup import BackupFetcher, default_backup_dir
from rag_agent.state import StateDB
from rag_agent.sync import SyncRunner


def name(day: int) -> str:
    return f"rag-202611{day:02d}.dump.age"


def read_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def agent_log(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "home" / "agent.log"
    logs.configure(path, secrets=[])
    yield path
    logs.close()


def test_downloads_missing_and_verifies_hash(
    root: Path, state: StateDB, tmp_path: Path, agent_log: Path
) -> None:
    write(root / "a.md", "본문")
    backup_dir = tmp_path / "backup"
    server = FakeSyncServer(root)
    server.backups = {
        name(12): b"backup twelve",
        name(13): b"backup thirteen",
        name(14): b"backup fourteen",
    }
    # 이미 있는 백업은 다시 받지 않는다(내용이 달라도 이름으로 판단)
    write(backup_dir / name(12), b"already here")
    # 목록의 해시와 실제 내용이 다르면 지우고 다음 실행에서 다시 받는다
    server.backup_sha[name(13)] = "0" * 64
    # 이름 규칙에 맞지 않는 항목은 무시한다(폴더 밖에 쓰지 않음)
    client = server.client()
    runner = SyncRunner(
        client, state, FakeLock(), backups=BackupFetcher(client, backup_dir), heartbeat_s=60
    )

    outcome = runner.run("cli")

    assert outcome.status == "succeeded"
    # 동기화가 끝난 뒤(complete 다음)에 목록을 조회하고 없는 것만 받는다
    calls = [f"{m} {p}" for m, p in server.calls if not p.endswith("/heartbeat")]
    complete_at = calls.index("POST /runs/1/complete")
    assert calls[complete_at + 1 :] == [
        "GET /backups/latest",
        f"GET /backups/{name(14)}",
        f"GET /backups/{name(13)}",
    ]
    b = outcome.backups
    assert b is not None
    assert b.downloaded == [name(14)]
    assert b.mismatched == [name(13)]
    assert b.skipped == 1
    assert (backup_dir / name(14)).read_bytes() == b"backup fourteen"
    assert (backup_dir / name(12)).read_bytes() == b"already here"
    assert not (backup_dir / name(13)).exists()
    assert not list(backup_dir.glob("*.part"))

    events = read_events(agent_log)
    got = [e for e in events if e["event"] == "agent.backup.downloaded"]
    assert len(got) == 1 and got[0]["file"] == name(14) and got[0]["size"] == 15
    assert got[0]["sha256_prefix"] == hashlib.sha256(b"backup fourteen").hexdigest()[:8]
    assert any(e["event"] == "agent.backup.hash_mismatch" and e["file"] == name(13) for e in events)

    # 다음 실행: 서버가 고친 해시로 다시 주면 받는다. 이미 받은 것은 다시 받지 않는다
    del server.backup_sha[name(13)]
    server.calls.clear()
    again = runner.run("cli")
    assert again.backups is not None and again.backups.downloaded == [name(13)]
    assert (backup_dir / name(13)).read_bytes() == b"backup thirteen"
    assert [p for m, p in server.calls if p.startswith("/backups/") and p != "/backups/latest"] == [
        f"/backups/{name(13)}"
    ]

    # 서버가 경로 조작 이름을 줘도 폴더 밖에 쓰지 않는다
    server.backups = {"../evil.dump.age": b"x", "rag-2026111.dump.age": b"y"}
    third = BackupFetcher(client, backup_dir).fetch()
    assert third.downloaded == [] and third.failed == []
    assert not (tmp_path / "evil.dump.age").exists()

    # 동기화가 실패하면 백업을 받지 않는다
    server.backups = {name(15): b"fifteen"}
    server.config = {}  # config 응답이 깨짐 → 실행 시작 전에 실패
    failed = runner.run("cli")
    assert failed.status == "failed" and failed.backups is None
    assert not (backup_dir / name(15)).exists()


def test_keeps_seven_newest(
    root: Path, state: StateDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_dir = tmp_path / "backup"
    # PC에 이미 6개(1~6일) + 규칙 밖 파일
    for day in range(1, 7):
        write(backup_dir / name(day), f"old {day}".encode())
    write(backup_dir / "readme.txt", "직접 둔 파일")
    server = FakeSyncServer(root)
    # VM에는 최근 3개(7~9일)
    server.backups = {name(d): f"new {d}".encode() for d in (7, 8, 9)}

    result = BackupFetcher(server.client(), backup_dir, logger=logging.getLogger("t")).fetch()

    assert sorted(result.downloaded) == [name(7), name(8), name(9)]
    # 9개 중 날짜순으로 가장 오래된 2개를 지운다
    assert result.pruned == [name(1), name(2)]
    remaining = sorted(p.name for p in backup_dir.iterdir())
    assert remaining == sorted([name(d) for d in range(3, 10)] + ["readme.txt"])

    # 설정값: 저장 위치는 RAG_AGENT_BACKUP_DIR, 없으면 Windows 기본 C:\Chanjin\dev\backup
    monkeypatch.setenv("RAG_AGENT_BACKUP_DIR", str(tmp_path / "elsewhere"))
    assert default_backup_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv("RAG_AGENT_BACKUP_DIR")
    monkeypatch.setattr("rag_agent.backup.sys.platform", "win32")
    assert str(default_backup_dir()) == r"C:\Chanjin\dev\backup"
