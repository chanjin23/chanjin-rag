"""Agent 로컬 상태 `state.db`(SQLite, TRD 6.1·10.1).

- `files`: 해시 캐시. (경로, 크기, 수정 시각)이 같으면 저장된 SHA-256을 재사용한다.
  `reveal_key`(path_key의 SHA-256)도 함께 저장해 `reveal`이 해시로 경로를 찾게 한다.
- `retry_queue`: 업로드 실패 목록. 다음 실행에서 manifest 판정을 다시 받아 자연스럽게 재시도하고,
  성공하거나 더 이상 업로드 판정이 나오지 않으면 지운다.
- `kv`: 서버 주소, 마지막 서버 설정(`reveal`의 범위 확인용), 마지막 실행 결과.
- `protocol_calls`: URL 프로토콜 반복 호출 제한. 호출마다 새 프로세스가 뜨므로 DB에 남긴다.
"""

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag_agent.rules import is_under

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path_key   TEXT PRIMARY KEY,
    path       TEXT NOT NULL,      -- manifest에 보낸 NFC 경로
    fs_path    TEXT NOT NULL,      -- 파일 시스템에서 읽은 그대로의 경로(열기·탐색기용)
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    reveal_key TEXT NOT NULL,
    seen_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_files_reveal_key ON files (reveal_key);
CREATE TABLE IF NOT EXISTS retry_queue (
    path_key   TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    attempts   INTEGER NOT NULL,
    first_at   REAL NOT NULL,
    last_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS protocol_calls (
    action TEXT NOT NULL,
    at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_protocol_calls ON protocol_calls (action, at);
"""

_FILE_COLUMNS = "path_key, path, fs_path, size, mtime_ns, sha256, reveal_key"


@dataclass(frozen=True)
class CachedFile:
    path_key: str
    path: str
    fs_path: str
    size: int
    mtime_ns: int
    sha256: str
    reveal_key: str


@dataclass(frozen=True)
class RetryItem:
    path: str
    reason: str
    attempts: int
    last_at: float


class StateDB:
    """SQLite 연결 하나. heartbeat 스레드는 DB를 쓰지 않으므로 메인 스레드에서만 쓴다."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- 해시 캐시 ---------------------------------------------------------

    def cached(self, path_key: str) -> CachedFile | None:
        row = self._conn.execute(
            f"SELECT {_FILE_COLUMNS} FROM files WHERE path_key = ?", (path_key,)
        ).fetchone()
        return CachedFile(*row) if row else None

    def put_files(self, files: Iterable[CachedFile], seen_at: float) -> None:
        rows = [
            (f.path_key, f.path, f.fs_path, f.size, f.mtime_ns, f.sha256, f.reveal_key, seen_at)
            for f in files
        ]
        with self.transaction() as c:
            c.executemany(
                f"INSERT INTO files ({_FILE_COLUMNS}, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (path_key) DO UPDATE SET path = excluded.path,"
                " fs_path = excluded.fs_path, size = excluded.size, mtime_ns = excluded.mtime_ns,"
                " sha256 = excluded.sha256, reveal_key = excluded.reveal_key,"
                " seen_at = excluded.seen_at",
                rows,
            )

    def prune_files(self, keep: set[str], protected: Iterable[str] = ()) -> int:
        """이번 스캔에서 보지 못한 항목을 지운다. `protected`(읽지 못한 경로) 아래는 남긴다."""
        guard = list(protected)
        rows = self._conn.execute("SELECT path_key, path FROM files").fetchall()
        drop = [
            (key,)
            for key, path in rows
            if key not in keep and not any(is_under(path, g) for g in guard)
        ]
        with self.transaction() as c:
            c.executemany("DELETE FROM files WHERE path_key = ?", drop)
        return len(drop)

    def find_by_reveal_key(self, key: str) -> CachedFile | None:
        row = self._conn.execute(
            f"SELECT {_FILE_COLUMNS} FROM files WHERE reveal_key = ?", (key,)
        ).fetchone()
        return CachedFile(*row) if row else None

    def file_count(self) -> int:
        return int(self._conn.execute("SELECT count(*) FROM files").fetchone()[0])

    # -- 재시도 큐 ---------------------------------------------------------

    def add_retry(self, path_key: str, path: str, reason: str, now: float) -> None:
        self._conn.execute(
            "INSERT INTO retry_queue (path_key, path, reason, attempts, first_at, last_at)"
            " VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT (path_key) DO UPDATE SET"
            " path = excluded.path, reason = excluded.reason,"
            " attempts = retry_queue.attempts + 1, last_at = excluded.last_at",
            (path_key, path, reason, now, now),
        )

    def remove_retry(self, path_keys: Iterable[str]) -> None:
        with self.transaction() as c:
            c.executemany("DELETE FROM retry_queue WHERE path_key = ?", [(k,) for k in path_keys])

    def retry_items(self) -> dict[str, RetryItem]:
        rows = self._conn.execute(
            "SELECT path_key, path, reason, attempts, last_at FROM retry_queue"
        ).fetchall()
        return {r[0]: RetryItem(path=r[1], reason=r[2], attempts=r[3], last_at=r[4]) for r in rows}

    # -- kv ----------------------------------------------------------------

    def get(self, key: str) -> Any:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # -- 프로토콜 반복 호출 제한 -----------------------------------------------

    def record_call_if_allowed(
        self, action: str, window_s: float, max_calls: int, now: float
    ) -> bool:
        """`window_s`초 안의 호출이 `max_calls`보다 적으면 기록하고 True, 아니면 기록 없이 False."""
        with self.transaction() as c:
            c.execute("DELETE FROM protocol_calls WHERE at < ?", (now - 3600,))
            (count,) = c.execute(
                "SELECT count(*) FROM protocol_calls WHERE action = ? AND at > ?",
                (action, now - window_s),
            ).fetchone()
            if count >= max_calls:
                return False
            c.execute("INSERT INTO protocol_calls (action, at) VALUES (?, ?)", (action, now))
            return True
