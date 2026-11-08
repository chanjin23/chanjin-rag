"""스캔·해시 캐시 (TRD 6.1 Agent 쪽 규칙, 14장 Agent). 파일은 모두 tmp_path의 합성 파일."""

import os
import sys
import unicodedata
from pathlib import Path
from typing import IO

import pytest
from fakes import write

from rag_agent.keys import make_path_key, reveal_key
from rag_agent.rules import SyncRules
from rag_agent.scan import Scanner, open_binary, plan_roots
from rag_agent.state import StateDB


def _paths(root: Path, scanned: list[str]) -> set[str]:
    base = unicodedata.normalize("NFC", str(root))
    return {p[len(base) + 1 :].replace("\\", "/") for p in scanned}


def test_fixed_excludes_are_root_anchored(root: Path, rules: SyncRules, state: StateDB) -> None:
    write(root / "project" / "a.md")
    write(root / "backup" / "rag-20261101.md")
    write(root / "Project" / "upper.md")  # 대소문자 무시
    write(root / "notes" / "project" / "b.md")  # 하위의 같은 이름 폴더는 빼지 않는다
    write(root / "notes" / "backup" / "c.md")
    write(root / "notes" / "node_modules" / "pkg" / "d.md")  # 기본 제외(어느 깊이든)
    write(root / "notes" / "~$draft.docx")
    write(root / "notes" / "scratch.tmp")
    write(root / "keep.md")

    result = Scanner(rules, state).scan()

    assert _paths(root, [f.path for f in result.files]) == {
        "notes/project/b.md",
        "notes/backup/c.md",
        "keep.md",
    }
    assert result.excluded >= 4
    assert result.scan_errors == []


def test_extension_case_insensitive_and_skip_stats(
    root: Path, rules: SyncRules, state: StateDB
) -> None:
    write(root / "a.PDF", b"%PDF-1.4 fake")
    write(root / "b.Md")
    write(root / "c.TXT")
    write(root / "d.docx", b"PK fake")
    write(root / "old.doc", b"x")
    write(root / "old2.DOC", b"x")
    write(root / "sheet.xls", b"x")
    write(root / "deck.ppt", b"x")
    write(root / "README")

    result = Scanner(rules, state).scan()

    assert _paths(root, [f.path for f in result.files]) == {"a.PDF", "b.Md", "c.TXT", "d.docx"}
    assert dict(result.skipped_formats) == {".doc": 2, ".xls": 1, ".ppt": 1, "(없음)": 1}
    assert result.stats()["skipped_formats"] == {
        "(없음)": 1,
        ".doc": 2,
        ".ppt": 1,
        ".xls": 1,
    }


class CountingOpener:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def __call__(self, path: str) -> IO[bytes]:
        self.opened.append(path)
        return open_binary(path)


def test_hash_cache_reuse(root: Path, rules: SyncRules, state: StateDB) -> None:
    a = write(root / "a.md", "첫 내용")
    write(root / "sub" / "b.txt", "둘째")
    opener = CountingOpener()

    first = Scanner(rules, state, opener).scan()
    assert (first.hashed, first.hash_reused) == (2, 0)
    state.put_files([f.cached() for f in first.files], seen_at=1.0)
    # path_key 해시(reveal 용)도 저장된다
    a_entry = next(f for f in first.files if f.path.endswith("a.md"))
    cached = state.find_by_reveal_key(reveal_key(a_entry.path))
    assert cached is not None and cached.path_key == make_path_key(a_entry.path)

    opener.opened.clear()
    second = Scanner(rules, state, opener).scan()
    assert (second.hashed, second.hash_reused) == (0, 2)
    assert opener.opened == []  # 같은 (경로, 크기, 수정 시각)이면 파일을 열지 않는다
    assert {f.sha256 for f in second.files} == {f.sha256 for f in first.files}

    # 내용과 수정 시각이 바뀐 파일만 다시 계산한다
    a.write_text("바뀐 내용입니다", encoding="utf-8")
    st = a.stat()
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    third = Scanner(rules, state, opener).scan()
    assert (third.hashed, third.hash_reused) == (1, 1)
    assert len(opener.opened) == 1 and opener.opened[0].endswith("a.md")
    changed = next(f for f in third.files if f.path.endswith("a.md"))
    assert changed.sha256 != a_entry.sha256


def test_locked_file_reported_as_scan_error(
    root: Path, rules: SyncRules, state: StateDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "open.md", "다른 프로그램이 잠근 파일")
    write(root / "ok.md")
    write(root / "secret" / "x.md")

    def opener(path: str) -> IO[bytes]:
        if path.endswith("open.md"):
            raise PermissionError(13, "The process cannot access the file", path)
        return open_binary(path)

    real_scandir = os.scandir

    def scandir(path: str) -> "os._ScandirIterator[str]":  # type: ignore[name-defined]
        if str(path).endswith("secret"):
            raise PermissionError(13, "Access is denied", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    result = Scanner(rules, state, opener).scan()

    assert _paths(root, [f.path for f in result.files]) == {"ok.md"}
    assert _paths(root, result.scan_errors) == {"open.md", "secret"}
    assert result.stats()["scan_errors"] == 2


def test_paths_nfc_normalized(root: Path, rules: SyncRules, state: StateDB) -> None:
    nfd_dir = unicodedata.normalize("NFD", "회의록")
    nfd_name = unicodedata.normalize("NFD", "한글문서.md")
    nfd_file = write(root / nfd_dir / nfd_name, "자모 분리된 파일명")

    result = Scanner(rules, state).scan()

    assert len(result.files) == 1
    item = result.files[0]
    expected = unicodedata.normalize("NFC", str(root / "회의록" / "한글문서.md"))
    assert item.path == expected
    assert unicodedata.is_normalized("NFC", item.manifest_entry()["path"])
    # 파일은 파일 시스템 경로 그대로 연다(NFD 이름도 읽힘)
    assert os.path.isfile(item.fs_path) and Path(item.fs_path).samefile(nfd_file)
    assert item.path_key == make_path_key(expected)


def _link_dir(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))  # 관리자 권한 없이 만들 수 있다


def test_links_outside_root_not_followed(
    tmp_path: Path, root: Path, rules: SyncRules, state: StateDB
) -> None:
    outside = tmp_path / "outside"
    write(outside / "leak.md", "루트 밖")
    write(root / "inside" / "in.md")
    _link_dir(root / "to-outside", outside)
    _link_dir(root / "to-inside", root / "inside")  # 루트 안을 가리키면 따라가되 순환하지 않는다

    result = Scanner(rules, state).scan()

    paths = _paths(root, [f.path for f in result.files])
    assert "to-outside/leak.md" not in paths
    assert "inside/in.md" in paths
    assert result.links_skipped == 1


def test_scan_roots_limit_reports_unscanned_server_roots(root: Path) -> None:
    other = root / "other"
    rules = SyncRules(allowed_root=str(root), roots=[str(root / "a"), str(other)])
    roots, uncovered = plan_roots(rules, [str(root / "a" / "sub"), "C:\\elsewhere"])
    assert roots == [str(root / "a" / "sub")]  # 서버 루트 밖 값은 버린다(넓힐 수 없음)
    assert set(uncovered) == {str(root / "a"), str(other)}


def test_reveal_key_matches_backend_path_key() -> None:
    from app.db.keys import make_path_key as backend_path_key

    for p in ["C:\\Chanjin\\dev\\Notes\\회의.MD", unicodedata.normalize("NFD", "C:\\가\\İ.txt")]:
        assert make_path_key(p) == backend_path_key(p)
