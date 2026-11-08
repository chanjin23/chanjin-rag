"""Agent 로컬 상태 폴더 (TRD 10.1 로컬 상태).

`%LOCALAPPDATA%/chanjin-rag`에 `state.db`(SQLite)와 `agent.log`를 둔다.
테스트·E2E는 `RAG_AGENT_HOME`으로 임시 폴더를 준다.
"""

import os
from pathlib import Path

APP_DIR_NAME = "chanjin-rag"


def agent_home() -> Path:
    override = os.environ.get("RAG_AGENT_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_DIR_NAME


def state_db_path(home: Path) -> Path:
    return home / "state.db"


def log_path(home: Path) -> Path:
    return home / "agent.log"
