"""백업 수신 (PRD 3장 DB 백업, TRD 10.3, 12.2).

동기화가 끝나면 `GET /sync/backups/latest`로 VM의 최근 백업 목록(파일명, 크기, SHA-256)을 받는다.

- 백업 폴더(기본 `C:\\Chanjin\\dev\\backup`, `RAG_AGENT_BACKUP_DIR`로 바꿈)에 없는 파일만
  내려받는다.
- 받는 중에는 `<이름>.part`에 쓰고, SHA-256이 목록과 같을 때만 원래 이름으로 옮긴다. 다르면 지우고
  다음 실행에서 다시 받는다.
- 이름 규칙(`rag-YYYYMMDD.dump.age`)에 맞지 않는 항목은 무시한다(서버가 준 이름으로 폴더 밖에
  쓰지 않게).
- 날짜순으로 7개(`KEEP_ON_PC`)를 넘는 오래된 백업은 지운다. 규칙에 맞지 않는 파일은 건드리지 않는다.
- 이 폴더는 서버의 고정 제외(`/backup/`)라 다시 업로드되지 않는다.

로그: `agent.backup.downloaded`(파일명 `file`, 크기, 해시 앞 8자리), `agent.backup.hash_mismatch`,
`agent.backup.failed`, `agent.backup.pruned`, `agent.backup.checked`.
"""

import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from rag_agent.client import ApiError, SyncClient
from rag_agent.home import agent_home
from rag_agent.logs import event, get_logger

BACKUP_NAME = re.compile(r"^rag-(\d{8})\.dump\.age$")
KEEP_ON_PC = 7
WINDOWS_BACKUP_DIR = r"C:\Chanjin\dev\backup"
CHUNK = 1024 * 1024


def default_backup_dir() -> Path:
    """설정값 `RAG_AGENT_BACKUP_DIR`, 없으면 Windows는 `C:\\Chanjin\\dev\\backup`.

    Windows가 아닌 곳(개발·테스트)에서는 상태 폴더 아래 `backup`을 쓴다.
    """
    override = os.environ.get("RAG_AGENT_BACKUP_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(WINDOWS_BACKUP_DIR)
    return agent_home() / "backup"


@dataclass
class BackupFetchResult:
    downloaded: list[str] = field(default_factory=list)
    skipped: int = 0  # 이미 있음
    mismatched: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    error: str | None = None  # 목록 조회 자체가 실패

    def as_dict(self) -> dict[str, Any]:
        return {
            "downloaded": len(self.downloaded),
            "skipped": self.skipped,
            "mismatched": len(self.mismatched),
            "failed": len(self.failed),
            "pruned": len(self.pruned),
        }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def local_backups(directory: Path) -> list[Path]:
    """폴더의 백업 파일(이름 규칙에 맞는 것), 날짜 오름차순."""
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.iterdir() if BACKUP_NAME.match(p.name) and p.is_file()),
        key=lambda p: p.name,
    )


def prune_local(directory: Path, keep: int = KEEP_ON_PC) -> list[str]:
    files = local_backups(directory)
    old = files[: max(len(files) - keep, 0)]
    for p in old:
        p.unlink(missing_ok=True)
    return [p.name for p in old]


class BackupFetcher:
    def __init__(
        self,
        client: SyncClient,
        directory: Path,
        *,
        keep: int = KEEP_ON_PC,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.directory = directory
        self.keep = keep
        self.log = logger or get_logger()

    def fetch(self, run_id: int | None = None) -> BackupFetchResult:
        """없는 백업을 내려받고 오래된 것을 정리한다. 예외를 밖으로 내보내지 않는다."""
        result = BackupFetchResult()
        try:
            listed = self.client.list_backups()
        except (ApiError, httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            result.error = e.code if isinstance(e, ApiError) else type(e).__name__
            event(
                self.log,
                "agent.backup.failed",
                logging.WARNING,
                run_id=run_id,
                stage="list",
                error=result.error,
            )
            return result
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            result.error = type(e).__name__
            event(
                self.log,
                "agent.backup.failed",
                logging.WARNING,
                run_id=run_id,
                stage="mkdir",
                error=result.error,
            )
            return result

        for item in listed:
            name = str(item.get("name", ""))
            expected = str(item.get("sha256", "")).lower()
            if not BACKUP_NAME.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                continue
            target = self.directory / name
            if target.exists():
                result.skipped += 1
                continue
            self._download(name, expected, target, result, run_id)

        result.pruned = prune_local(self.directory, self.keep)
        if result.pruned:
            event(self.log, "agent.backup.pruned", run_id=run_id, count=len(result.pruned))
        event(self.log, "agent.backup.checked", run_id=run_id, **result.as_dict())
        return result

    def _download(
        self,
        name: str,
        expected: str,
        target: Path,
        result: BackupFetchResult,
        run_id: int | None,
    ) -> None:
        part = target.with_name(name + ".part")
        try:
            size = self.client.download_backup(name, part)
            actual = _sha256(part)
        except (ApiError, httpx.HTTPError, OSError) as e:
            part.unlink(missing_ok=True)
            result.failed.append(name)
            event(
                self.log,
                "agent.backup.failed",
                logging.WARNING,
                run_id=run_id,
                stage="download",
                file=name,
                error=e.code if isinstance(e, ApiError) else type(e).__name__,
            )
            return
        if actual != expected:
            # 검증 실패: 지우고 다음 실행에서 다시 받는다
            part.unlink(missing_ok=True)
            result.mismatched.append(name)
            event(
                self.log,
                "agent.backup.hash_mismatch",
                logging.WARNING,
                run_id=run_id,
                file=name,
                sha256_prefix=expected[:8],
            )
            return
        os.replace(part, target)
        result.downloaded.append(name)
        event(
            self.log,
            "agent.backup.downloaded",
            run_id=run_id,
            file=name,
            size=size,
            sha256_prefix=actual[:8],
        )
