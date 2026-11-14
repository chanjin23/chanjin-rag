"""DB 암호화 백업 (PRD 3장 DB 백업, TRD 12.2).

매일 03:00(한국 시간) Worker의 APScheduler가 `create_backup`을 부른다.

1. `pg_dump -Fc -f <임시 평문 덤프> --dbname=<DB>` (비밀번호는 명령줄이 아니라 `PGPASSWORD`로)
2. `age -r <BACKUP_AGE_RECIPIENT> -o <임시 암호문> <평문 덤프>`
3. 임시 암호문을 `rag-YYYYMMDD.dump.age`(한국 시간 날짜)로 옮기고 SHA-256을 옆 파일
   `rag-YYYYMMDD.dump.age.sha256`에 적는다. 평문 덤프는 성공·실패와 상관없이 지운다.
4. 날짜순으로 최근 `BACKUP_KEEP_ON_VM`(기본 3)개만 남긴다.

외부 명령은 `CommandRunner`로 감싸 주입한다(테스트는 가짜 실행기로 명령과 순서를 확인).
명령 자체는 설정 `BACKUP_PG_DUMP_CMD`·`BACKUP_AGE_CMD`(JSON 목록, 기본 `["pg_dump"]`·`["age"]`).

로그: `backup.created`(파일명, 크기, 남긴·지운 개수), `backup.failed`(단계, 원인 코드).
DB 주소·비밀번호·공개키는 로그에 남기지 않는다.
"""

import hashlib
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy.engine import URL, make_url

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

BACKUP_NAME = re.compile(r"^rag-(\d{8})\.dump\.age$")
SHA_SUFFIX = ".sha256"
CHUNK = 1024 * 1024

Stage = Literal["config", "pg_dump", "age", "store"]


class CommandFailed(Exception):
    """외부 명령이 0이 아닌 코드로 끝났거나 실행되지 않았다."""

    def __init__(self, program: str, returncode: int | None) -> None:
        super().__init__(f"{program} exited with {returncode}")
        self.program = program
        self.returncode = returncode


# (명령 인자 목록, 추가 환경 변수) → 실패하면 CommandFailed
CommandRunner = Callable[[Sequence[str], Mapping[str, str]], None]


def subprocess_runner(args: Sequence[str], env: Mapping[str, str]) -> None:
    """실제 실행기. 출력은 버리지 않고 받아만 두며(비밀값이 섞일 수 있어) 로그에 남기지 않는다."""
    try:
        proc = subprocess.run(  # noqa: S603 - 인자 목록 실행, 셸 없음
            list(args),
            env={**os.environ, **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except OSError as e:
        raise CommandFailed(Path(args[0]).name, None) from e
    if proc.returncode != 0:
        raise CommandFailed(Path(args[0]).name, proc.returncode)


class BackupError(Exception):
    def __init__(self, stage: Stage, code: str) -> None:
        super().__init__(f"{stage}: {code}")
        self.stage = stage
        self.code = code


@dataclass(frozen=True)
class BackupInfo:
    name: str
    size: int
    sha256: str
    created_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class BackupResult:
    info: BackupInfo
    removed: list[str]


def backup_name(now: datetime, tz: ZoneInfo) -> str:
    return f"rag-{now.astimezone(tz):%Y%m%d}.dump.age"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def pg_dump_target(database_url: str) -> tuple[str, dict[str, str]]:
    """SQLAlchemy URL → (비밀번호를 뺀 libpq URL, `PGPASSWORD` 환경 변수)."""
    url = make_url(database_url)
    env = {"PGPASSWORD": url.password} if url.password else {}
    plain = URL.create(
        "postgresql",
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    )
    return plain.render_as_string(hide_password=False), env


def backup_files(directory: Path) -> list[Path]:
    """백업 파일(이름 규칙에 맞는 것만), 날짜 오름차순."""
    if not directory.is_dir():
        return []
    found = [
        p
        for p in directory.iterdir()
        if BACKUP_NAME.match(p.name) and p.is_file() and not p.is_symlink()
    ]
    return sorted(found, key=lambda p: p.name)


def read_info(path: Path) -> BackupInfo:
    """백업 파일 정보. SHA-256은 옆 파일에서 읽고, 없거나 형식이 틀리면 계산해 다시 적는다."""
    sidecar = path.with_name(path.name + SHA_SUFFIX)
    digest = ""
    try:
        digest = sidecar.read_text(encoding="ascii").strip().split()[0].lower()
    except (OSError, IndexError, UnicodeDecodeError):
        digest = ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = file_sha256(path)
        sidecar.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    stat = path.stat()
    return BackupInfo(
        name=path.name,
        size=stat.st_size,
        sha256=digest,
        created_at=datetime.fromtimestamp(stat.st_mtime, UTC),
    )


def list_backups(directory: Path) -> list[BackupInfo]:
    """VM에 있는 백업 목록, 최신 먼저."""
    return [read_info(p) for p in reversed(backup_files(directory))]


def latest_backup(directory: Path) -> tuple[str, datetime] | None:
    """가장 새 백업의 (파일명, 수정 시각). 없으면 None."""
    files = backup_files(directory)
    if not files:
        return None
    return files[-1].name, datetime.fromtimestamp(files[-1].stat().st_mtime, UTC)


def resolve_backup(directory: Path, name: str) -> Path | None:
    """내려받을 백업 파일 경로. 이름 규칙에 맞고 백업 폴더 바로 아래에 실제로 있을 때만.

    경로 구분자·`..`·절대 경로 등은 이름 규칙에서 걸러지고, 마지막으로 실제 경로가 폴더 안인지 본다.
    """
    if not BACKUP_NAME.fullmatch(name):
        return None
    base = directory.resolve()
    candidate = (base / name).resolve()
    if candidate.parent != base or not candidate.is_file():
        return None
    return candidate


def prune_backups(directory: Path, keep: int) -> list[str]:
    """날짜순 최근 `keep`개만 남기고 지운다(옆 파일 포함). 지운 파일명."""
    files = backup_files(directory)
    old = files[: max(len(files) - max(keep, 1), 0)]
    for p in old:
        p.unlink(missing_ok=True)
        p.with_name(p.name + SHA_SUFFIX).unlink(missing_ok=True)
    return [p.name for p in old]


def _remove(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def create_backup(
    settings: Settings,
    *,
    now: datetime,
    runner: CommandRunner = subprocess_runner,
) -> BackupResult:
    """백업 하나를 만들고 오래된 것을 정리한다. 실패하면 `backup.failed`를 남기고 BackupError."""
    directory = settings.backup_path
    tz = ZoneInfo(settings.tz)
    name = backup_name(now, tz)
    try:
        return _create(settings, directory, name, now, runner)
    except BackupError as e:
        log.error("backup.failed", stage=e.stage, error=e.code, name=name)
        raise


def _create(
    settings: Settings,
    directory: Path,
    name: str,
    now: datetime,
    runner: CommandRunner,
) -> BackupResult:
    recipient = (settings.backup_age_recipient or "").strip()
    if not recipient:
        raise BackupError("config", "no_recipient")
    if not settings.backup_pg_dump_cmd or not settings.backup_age_cmd:
        raise BackupError("config", "no_command")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BackupError("store", type(e).__name__) from e

    stamp = f"{now:%Y%m%d%H%M%S}-{os.getpid()}"
    plain = directory / f".rag-{stamp}.dump.tmp"
    encrypted = directory / f".rag-{stamp}.age.part"
    final = directory / name
    target, env = pg_dump_target(settings.database_url)
    try:
        try:
            runner(
                [*settings.backup_pg_dump_cmd, "-Fc", "-f", str(plain), f"--dbname={target}"],
                env,
            )
        except CommandFailed as e:
            raise BackupError("pg_dump", f"exit_{e.returncode}") from e
        try:
            runner(
                [*settings.backup_age_cmd, "-r", recipient, "-o", str(encrypted), str(plain)],
                {},
            )
        except CommandFailed as e:
            raise BackupError("age", f"exit_{e.returncode}") from e
        if not encrypted.is_file():
            raise BackupError("age", "no_output")
        try:
            digest = file_sha256(encrypted)
            os.replace(encrypted, final)
            final.with_name(final.name + SHA_SUFFIX).write_text(
                f"{digest}  {final.name}\n", encoding="ascii"
            )
        except OSError as e:
            raise BackupError("store", type(e).__name__) from e
    finally:
        # 평문 덤프는 VM에 남기지 않는다
        _remove(plain, encrypted)

    removed = prune_backups(directory, settings.backup_keep_on_vm)
    info = read_info(final)
    log.info(
        "backup.created",
        name=info.name,
        size=info.size,
        sha256_prefix=info.sha256[:8],
        kept=len(backup_files(directory)),
        removed=len(removed),
    )
    return BackupResult(info=info, removed=removed)
