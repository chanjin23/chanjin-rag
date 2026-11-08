"""Agent 테스트 공용 fixture: 임시 수집 루트, 임시 state.db, 기본 규칙."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from rag_agent.rules import SyncRules
from rag_agent.state import StateDB


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "dev"
    r.mkdir()
    return r


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDB]:
    db = StateDB(tmp_path / "home" / "state.db")
    yield db
    db.close()


@pytest.fixture
def rules(root: Path) -> SyncRules:
    return SyncRules(
        allowed_root=str(root),
        roots=[str(root)],
        excludes=["node_modules/", ".git/", "~$*", "*.tmp"],
    )
