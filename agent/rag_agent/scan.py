"""폴더 스캔과 해시 캐시 (TRD 6.1 Agent 쪽 규칙).

- 서버 설정의 `roots`를 순회한다. 고정 제외·제외 패턴에 걸린 폴더는 들어가지 않는다.
- 지원 확장자만 manifest에 넣고(대소문자 무시), 나머지는 형식별 건너뛴 수를 센다.
- 경로는 NFC로 정규화해 보낸다. 파일은 파일 시스템이 준 경로(`fs_path`) 그대로 연다
  (NFD 이름의 파일을 NFC 경로로 열면 찾지 못하므로).
- 수집 루트 밖을 가리키는 심볼릭 링크·정션은 따라가지 않는다.
- (경로, 크기, 수정 시각)이 캐시와 같으면 저장된 해시를 쓰고, 바뀐 파일만 SHA-256을 계산한다.
- 열지 못한 파일(다른 프로그램이 잠금)과 읽지 못한 폴더(권한 오류)는 manifest에 넣지 않고
  `scan_errors`로 보고한다. 서버는 그 아래 문서를 삭제로 판정하지 않는다.
"""

import hashlib
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any

from rag_agent.keys import make_path_key, nfc, reveal_key
from rag_agent.rules import SyncRules, extension, is_under
from rag_agent.state import CachedFile, StateDB

Opener = Callable[[str], IO[bytes]]

_CHUNK = 1024 * 1024


def open_binary(path: str) -> IO[bytes]:
    return open(path, "rb")  # noqa: SIM115 - 호출한 쪽이 with로 닫는다


@dataclass(frozen=True)
class ScannedFile:
    path: str  # NFC, manifest·업로드에 쓰는 경로
    fs_path: str  # 파일 시스템 경로(열기용)
    size: int
    mtime_ns: int
    sha256: str

    @property
    def path_key(self) -> str:
        return make_path_key(self.path)

    @property
    def mtime_iso(self) -> str:
        return datetime.fromtimestamp(self.mtime_ns / 1e9, UTC).isoformat()

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "mtime": self.mtime_iso,
            "sha256": self.sha256,
        }

    def cached(self) -> CachedFile:
        return CachedFile(
            path_key=self.path_key,
            path=self.path,
            fs_path=self.fs_path,
            size=self.size,
            mtime_ns=self.mtime_ns,
            sha256=self.sha256,
            reveal_key=reveal_key(self.path),
        )


@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    scan_errors: list[str] = field(default_factory=list)
    skipped_formats: Counter[str] = field(default_factory=Counter)
    excluded: int = 0
    links_skipped: int = 0
    hashed: int = 0
    hash_reused: int = 0

    def stats(self) -> dict[str, Any]:
        return {
            "files": len(self.files),
            "hashed": self.hashed,
            "hash_reused": self.hash_reused,
            "skipped_formats": dict(sorted(self.skipped_formats.items())),
            "excluded": self.excluded,
            "links_skipped": self.links_skipped,
            "scan_errors": len(self.scan_errors),
        }


def sha256_file(path: str, opener: Opener = open_binary) -> str:
    h = hashlib.sha256()
    with opener(path) as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def plan_roots(rules: SyncRules, limit: list[str] | None) -> tuple[list[str], list[str]]:
    """(스캔할 루트, 스캔하지 않는 서버 루트). `limit`(개발·E2E용)은 서버 루트를 좁히기만 한다.

    스캔하지 않는 서버 루트는 `scan_errors`로 보내 그 아래 문서가 삭제로 판정되지 않게 한다.
    """
    if not limit:
        roots = [r for r in rules.roots if rules.in_scope(r)]
        return _dedupe_nested(roots), []
    roots = [r for r in limit if rules.in_scope(r)]
    uncovered = [r for r in rules.roots if not any(is_under(r, lim) for lim in roots)]
    return _dedupe_nested(roots), uncovered


def _dedupe_nested(roots: list[str]) -> list[str]:
    """다른 루트의 하위인 루트는 뺀다(같은 파일을 두 번 보지 않게)."""
    out: list[str] = []
    for r in sorted(roots, key=len):
        if not any(is_under(r, kept) for kept in out):
            out.append(r)
    return out


class Scanner:
    def __init__(self, rules: SyncRules, state: StateDB, opener: Opener = open_binary) -> None:
        self.rules = rules
        self.state = state
        self.opener = opener

    def scan(self, limit_roots: list[str] | None = None) -> ScanResult:
        result = ScanResult()
        roots, uncovered = plan_roots(self.rules, limit_roots)
        result.scan_errors.extend(nfc(r) for r in uncovered)
        seen: set[str] = set()
        for root in roots:
            self._walk_root(root, result, seen)
        return result

    def _walk_root(self, root: str, result: ScanResult, seen: set[str]) -> None:
        if self.rules.excluded(root, is_dir=True):
            return
        if not os.path.isdir(root):
            # 설정된 루트가 없거나 읽을 수 없으면 삭제로 오판하지 않도록 보고한다
            result.scan_errors.append(nfc(root))
            return
        real_root = os.path.realpath(root)
        visited: set[str] = {os.path.normcase(real_root)}
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                with os.scandir(folder) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                result.scan_errors.append(nfc(folder))
                continue
            for entry in entries:
                self._visit(entry, real_root, visited, stack, result, seen)

    def _visit(
        self,
        entry: os.DirEntry[str],
        real_root: str,
        visited: set[str],
        stack: list[str],
        result: ScanResult,
        seen: set[str],
    ) -> None:
        fs_path = entry.path
        path = nfc(fs_path)
        try:
            is_link = entry.is_symlink() or entry.is_junction()
            if is_link:
                target = os.path.realpath(fs_path)
                if not is_under(target, real_root):
                    result.links_skipped += 1
                    return
            is_dir = entry.is_dir()
            is_file = not is_dir and entry.is_file()
        except OSError:
            result.scan_errors.append(path)
            return
        if is_dir:
            if self.rules.excluded(path, is_dir=True):
                result.excluded += 1
                return
            real = os.path.normcase(os.path.realpath(fs_path))
            if real in visited:  # 루트 안 링크가 만드는 순환
                return
            visited.add(real)
            stack.append(fs_path)
            return
        if not is_file:
            return
        if self.rules.excluded(path):
            result.excluded += 1
            return
        if not self.rules.supported(entry.name):
            result.skipped_formats[extension(entry.name) or "(없음)"] += 1
            return
        key = make_path_key(path)
        if key in seen:
            return
        scanned = self._file(fs_path, path, key, result)
        if scanned is not None:
            seen.add(key)
            result.files.append(scanned)

    def _file(self, fs_path: str, path: str, key: str, result: ScanResult) -> ScannedFile | None:
        try:
            st = os.stat(fs_path)
        except OSError:
            result.scan_errors.append(path)
            return None
        cached = self.state.cached(key)
        if cached is not None and cached.size == st.st_size and cached.mtime_ns == st.st_mtime_ns:
            result.hash_reused += 1
            digest = cached.sha256
        else:
            try:
                digest = sha256_file(fs_path, self.opener)
            except OSError:
                # 다른 프로그램이 잠근 파일 등: 이번 실행에서 건너뛰고 삭제로 오판하지 않게 보고
                result.scan_errors.append(path)
                return None
            result.hashed += 1
        return ScannedFile(
            path=path, fs_path=fs_path, size=st.st_size, mtime_ns=st.st_mtime_ns, sha256=digest
        )
