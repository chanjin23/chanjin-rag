"""DB 암호화 백업과 백업 API (PRD 3장 DB 백업, TRD 8.2, 12.2).

외부 명령(`pg_dump`, `age`)은 가짜 실행기로 바꿔, 실행할 명령·순서·환경 변수와 결과 파일을 확인한다.
가짜 실행기는 실제 명령처럼 `-f`·`-o`로 받은 경로에 파일을 쓴다. 백업 파일 내용은 합성 바이트다.
"""

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from apscheduler.triggers.cron import CronTrigger
from structlog.testing import capture_logs

from app.core.config import Settings, get_settings
from app.lifecycle.backup import BackupError, CommandFailed, create_backup, prune_backups

RECIPIENT = "age1testrecipientxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
DB_URL = "postgresql+psycopg://rag:s3cret@db.internal:5432/rag"
# 한국 시간 2026-11-15 03:00 = UTC 11-14 18:00
NOW = datetime(2026, 11, 14, 18, 0, tzinfo=UTC)


class FakeRunner:
    """`pg_dump`·`age` 대신 인자를 기록하고 출력 파일을 만든다.

    `fail`에 프로그램 이름을 주면 그 명령이 실패한다.
    """

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.fail = fail
        self.plain_seen: list[Path] = []

    def __call__(self, args: Sequence[str], env: Mapping[str, str]) -> None:
        argv = list(args)
        self.calls.append((argv, dict(env)))
        program = Path(argv[0]).name
        if program == self.fail:
            raise CommandFailed(program, 2)
        if program == "pg_dump":
            out = Path(argv[argv.index("-f") + 1])
            out.write_bytes(b"PGDMP fake custom-format dump")
        elif program == "age":
            out = Path(argv[argv.index("-o") + 1])
            plain = Path(argv[-1])
            self.plain_seen.append(plain)
            out.write_bytes(b"age-encryption.org/v1\n" + plain.read_bytes()[::-1])


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "provider": "fake",
        "database_url": DB_URL,
        "backup_dir": str(tmp_path / "backups"),
        "backup_age_recipient": RECIPIENT,
        "backup_keep_on_vm": 3,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


def test_backup_pipeline_commands(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    runner = FakeRunner()
    with capture_logs() as logs:
        result = create_backup(settings, now=NOW, runner=runner)

    # 1) pg_dump -Fc → 2) age -r <공개키>
    assert [Path(c[0][0]).name for c in runner.calls] == ["pg_dump", "age"]
    dump_args, dump_env = runner.calls[0]
    assert dump_args[1:3] == ["-Fc", "-f"]
    plain = Path(dump_args[3])
    assert plain.parent == tmp_path / "backups"
    assert dump_args[4] == "--dbname=postgresql://rag@db.internal:5432/rag"
    # 비밀번호는 명령줄이 아니라 PGPASSWORD로
    assert dump_env == {"PGPASSWORD": "s3cret"}
    assert "s3cret" not in " ".join(dump_args)

    age_args, _ = runner.calls[1]
    assert age_args[1:3] == ["-r", RECIPIENT]
    assert age_args[3] == "-o"
    assert age_args[-1] == str(plain)

    # 결과: backups/rag-YYYYMMDD.dump.age(한국 시간 날짜), 평문 덤프는 남지 않는다
    final = tmp_path / "backups" / "rag-20261115.dump.age"
    assert result.info.name == final.name
    assert final.read_bytes().startswith(b"age-encryption.org/v1\n")
    digest = hashlib.sha256(final.read_bytes()).hexdigest()
    assert result.info.sha256 == digest
    assert result.info.size == final.stat().st_size
    assert (tmp_path / "backups" / "rag-20261115.dump.age.sha256").read_text().startswith(digest)
    assert not plain.exists()
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == [
        "rag-20261115.dump.age",
        "rag-20261115.dump.age.sha256",
    ]

    created = [e for e in logs if e["event"] == "backup.created"]
    assert len(created) == 1
    assert created[0]["name"] == "rag-20261115.dump.age"
    assert created[0]["size"] == final.stat().st_size
    assert RECIPIENT not in str(logs) and "s3cret" not in str(logs)

    # 실패: 단계별 backup.failed, 평문 덤프·임시 파일을 남기지 않는다
    for program in ("pg_dump", "age"):
        failing = FakeRunner(fail=program)
        with capture_logs() as logs, pytest.raises(BackupError) as exc:
            create_backup(settings, now=NOW + timedelta(days=1), runner=failing)
        assert exc.value.stage == program
        failed = [e for e in logs if e["event"] == "backup.failed"]
        assert failed and failed[0]["stage"] == program and failed[0]["error"] == "exit_2"
        assert not any(p.name.startswith(".") for p in (tmp_path / "backups").iterdir())
        assert not (tmp_path / "backups" / "rag-20261116.dump.age").exists()

    # 공개키가 없으면 명령을 실행하지 않고 실패
    no_key = make_settings(tmp_path, backup_age_recipient=None)
    idle = FakeRunner()
    with capture_logs() as logs, pytest.raises(BackupError):
        create_backup(no_key, now=NOW, runner=idle)
    assert idle.calls == []
    assert logs[-1]["event"] == "backup.failed" and logs[-1]["error"] == "no_recipient"

    # Worker의 매일 03:00(한국 시간) 예약 작업
    from app.db.models import EMBED_DIM
    from app.jobs.runner import JobRunner
    from app.jobs.worker import Worker
    from app.providers.fake import FakeEmbeddingProvider

    worker = Worker(
        JobRunner(None, FakeEmbeddingProvider(EMBED_DIM)),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    job = {j.id: j for j in worker.build_scheduler().get_jobs()}["backup"]
    assert isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("3", "0")
    assert str(job.trigger.timezone) == "Asia/Seoul"


def test_keeps_last_three_on_vm(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    runner = FakeRunner()
    names = []
    for day in range(5):
        result = create_backup(settings, now=NOW + timedelta(days=day), runner=runner)
        names.append(result.info.name)
    assert names == [f"rag-202611{d}.dump.age" for d in (15, 16, 17, 18, 19)]
    backups = tmp_path / "backups"
    kept = sorted(p.name for p in backups.iterdir() if p.name.endswith(".age"))
    assert kept == names[-3:]
    # SHA-256 옆 파일도 함께 지운다
    assert sorted(p.name for p in backups.iterdir() if p.name.endswith(".sha256")) == [
        n + ".sha256" for n in names[-3:]
    ]

    # 규칙에 맞지 않는 파일은 건드리지 않고, 남길 개수는 설정값을 따른다
    (backups / "notes.txt").write_text("keep me")
    assert prune_backups(backups, 1) == names[-3:-1]
    assert sorted(p.name for p in backups.iterdir()) == [
        "notes.txt",
        names[-1],
        names[-1] + ".sha256",
    ]


@pytest.fixture
def backup_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "vm-backups"
    directory.mkdir()
    monkeypatch.setenv("BACKUP_DIR", str(directory))
    get_settings.cache_clear()
    return directory


def write_backup(directory: Path, name: str, content: bytes) -> str:
    (directory / name).write_bytes(content)
    return hashlib.sha256(content).hexdigest()


async def test_backup_api_lists_sha256(
    sync_client: httpx.AsyncClient, backup_dir: Path, chat_client: httpx.AsyncClient
) -> None:
    # 백업이 없으면 빈 목록
    empty = await sync_client.get("/api/sync/backups/latest")
    assert empty.status_code == 200
    assert empty.json() == {"backups": []}

    old = write_backup(backup_dir, "rag-20261113.dump.age", b"old backup bytes")
    new = write_backup(backup_dir, "rag-20261114.dump.age", b"newer backup bytes!")
    (backup_dir / ".rag-x.dump.tmp").write_bytes(b"plain dump in progress")
    (backup_dir / "other.txt").write_text("x")

    with capture_logs() as logs:
        res = await sync_client.get("/api/sync/backups/latest")
    assert res.status_code == 200
    items = res.json()["backups"]
    assert [(i["name"], i["size"], i["sha256"]) for i in items] == [
        ("rag-20261114.dump.age", 19, new),
        ("rag-20261113.dump.age", 16, old),
    ]
    assert all(i["created_at"] for i in items)
    assert any(e["event"] == "backup.listed" and e["count"] == 2 for e in logs)

    # 내려받기: 내용 그대로, 서버 로그 backup.downloaded
    with capture_logs() as logs:
        got = await sync_client.get("/api/sync/backups/rag-20261114.dump.age")
    assert got.status_code == 200
    assert got.content == b"newer backup bytes!"
    assert hashlib.sha256(got.content).hexdigest() == new
    assert any(
        e["event"] == "backup.downloaded" and e["name"] == "rag-20261114.dump.age" for e in logs
    )

    # 동기화 화면의 최근 백업 시각은 가장 새 백업 파일
    status = (await chat_client.get("/api/sync/status")).json()
    assert status["last_backup_at"] is not None
    assert status["last_backup_name"] == "rag-20261114.dump.age"
    assert status["stale"]["backup"] is False

    # Agent 토큰이 없으면 거절
    del sync_client.headers["Authorization"]
    assert (await sync_client.get("/api/sync/backups/latest")).status_code == 401
    assert (await sync_client.get("/api/sync/backups/rag-20261114.dump.age")).status_code == 401


async def test_backup_download_blocks_path_traversal(
    sync_client: httpx.AsyncClient, backup_dir: Path, tmp_path: Path
) -> None:
    write_backup(backup_dir, "rag-20261114.dump.age", b"backup")
    # 백업 폴더 밖의 파일(이름 규칙에 맞게 지어도 폴더 밖이면 안 된다)
    (tmp_path / "secret.txt").write_text("secret")
    (tmp_path / "rag-20261101.dump.age").write_bytes(b"outside")
    (backup_dir / "notes.txt").write_text("not a backup")

    attempts = [
        "..%2Fsecret.txt",
        "..%2Frag-20261101.dump.age",
        "%2E%2E%2Frag-20261101.dump.age",
        "..%5Csecret.txt",
        "..%5Crag-20261101.dump.age",
        "%2Fetc%2Fpasswd",
        "notes.txt",
        "rag-20261114.dump.age.sha256",
        "rag-20261114.dump.age%00.txt",
        "rag-20261199.dump.age",  # 규칙엔 맞지만 없는 파일
        "..",
    ]
    for name in attempts:
        with capture_logs() as logs:
            res = await sync_client.get(f"/api/sync/backups/{name}")
        assert res.status_code == 404, name
        assert b"secret" not in res.content and b"outside" not in res.content
        if res.headers.get("content-type", "").startswith("application/json"):
            body = res.json()
            if "error" in body:
                assert body["error"]["code"] == "backup_not_found"
        assert not any(e["event"] == "backup.downloaded" for e in logs)

    # 폴더 안을 가리키는 심볼릭 링크라도 밖의 파일이면 거절
    link = backup_dir / "rag-20261102.dump.age"
    try:
        link.symlink_to(tmp_path / "secret.txt")
    except OSError:
        pass  # Windows에서 권한이 없으면 링크를 만들 수 없다(그 경우 위 검사로 충분)
    else:
        res = await sync_client.get("/api/sync/backups/rag-20261102.dump.age")
        assert res.status_code == 404
        assert b"secret" not in res.content

    ok = await sync_client.get("/api/sync/backups/rag-20261114.dump.age")
    assert ok.status_code == 200 and ok.content == b"backup"
