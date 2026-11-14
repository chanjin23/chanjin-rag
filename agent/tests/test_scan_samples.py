"""실제 문서 폴더(SAMPLE_DOCS_DIRS)를 Agent 스캐너로 훑어 깨지지 않는지 본다.

`pytest -m samples`로만 실행한다. 개인·고객사 문서이므로 내용·파일명·경로를 출력하거나 기록하지 않고
개수만 다룬다. 상태 DB는 tmp_path에 두고, 서버에는 아무것도 보내지 않는다.
"""

import os
import unicodedata
from pathlib import Path

import pytest

from rag_agent.rules import SyncRules
from rag_agent.scan import Scanner
from rag_agent.state import StateDB

pytestmark = pytest.mark.samples

DEFAULT_EXCLUDES = [".git/", "node_modules/", ".venv/", "venv/", "__pycache__/", "~$*", "*.tmp"]


def _sample_dirs() -> list[Path]:
    raw = os.environ.get("SAMPLE_DOCS_DIRS", "")
    return [Path(p) for p in raw.split(os.pathsep) if p.strip() and Path(p).is_dir()]


def test_scan_sample_folders_and_reuse_hash_cache(tmp_path: Path) -> None:
    dirs = _sample_dirs()
    if not dirs:
        pytest.fail("SAMPLE_DOCS_DIRS에 읽을 수 있는 폴더가 없습니다")
    total = 0
    with StateDB(tmp_path / "state.db") as state:
        for folder in dirs:
            rules = SyncRules(
                allowed_root=str(folder), roots=[str(folder)], excludes=DEFAULT_EXCLUDES
            )
            first = Scanner(rules, state).scan()
            assert first.files, "지원 형식 파일이 하나도 없음"
            assert all(unicodedata.is_normalized("NFC", f.path) for f in first.files)
            assert all(len(f.sha256) == 64 for f in first.files)
            assert len({f.path_key for f in first.files}) == len(first.files)
            # 잠긴 파일 등으로 못 읽은 항목은 아주 적어야 한다(전체의 5% 이하)
            assert len(first.scan_errors) <= max(1, len(first.files) // 20)
            state.put_files([f.cached() for f in first.files], seen_at=1.0)

            second = Scanner(rules, state).scan()
            # 바뀌지 않은 파일은 다시 읽지 않는다(그 사이 편집된 파일 몇 개는 허용)
            slack = len(first.scan_errors) + 3
            assert second.hashed <= slack
            assert second.hash_reused >= len(first.files) - slack
            total += len(first.files)
    print(f"\n[samples] 스캔 파일 {total}개, 폴더 {len(dirs)}개")
