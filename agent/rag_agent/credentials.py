"""Agent 토큰과 서버 주소 (TRD 10.1 토큰, 11장 Agent 토큰).

- 토큰: keyring(Windows 자격 증명 관리자)에 보관한다. 개발·E2E는 `RAG_AGENT_TOKEN`으로 줄 수 있다.
- 서버 주소: `rag-agent login --server`가 `state.db`에 저장한다. `RAG_AGENT_SERVER`가 있으면 그 값.
  URL 프로토콜로는 받지 않는다(TRD 10.2).
"""

import os

import keyring
from keyring.errors import KeyringError

from rag_agent.state import StateDB

KEYRING_SERVICE = "chanjin-rag"
KEYRING_USER = "agent-token"


def get_token() -> str | None:
    env = os.environ.get("RAG_AGENT_TOKEN")
    if env:
        return env
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except KeyringError:
        return None


def set_token(token: str) -> None:
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token)


def get_server(state: StateDB) -> str | None:
    env = os.environ.get("RAG_AGENT_SERVER")
    if env:
        return env
    value = state.get("server")
    return str(value) if value else None


def set_server(state: StateDB, server: str) -> None:
    state.set("server", server.rstrip("/"))
