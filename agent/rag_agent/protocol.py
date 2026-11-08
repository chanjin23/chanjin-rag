"""`chanjin-sync://` URL 프로토콜 처리 (TRD 10.2 URL 프로토콜 보안).

웹 페이지라면 어디서든 이 링크를 걸 수 있으므로 URL을 신뢰하지 않는다.
- 동작은 `run`, `reveal` 두 가지뿐이다. 정해진 매개변수 외에는 무시하고, 경로·명령·서버 주소는
  URL로 받지 않는다. 동기화 대상과 서버 주소는 항상 로컬 설정과 서버의 `sync_config`에서 가져온다.
- `run`: `source`(`web`·`cli`·`schedule`, 없으면 `web`)만 받는다.
- `reveal`: `k`(path_key의 SHA-256, 64자리 16진수)만 받는다. 로컬 `state.db`에서 경로를 찾고, 그
  경로가 현재 수집 범위 안이고 제외 패턴에 걸리지 않으며 실제로 있는 파일일 때만
  `explorer.exe /select,<경로>`를 인자 배열로 실행한다(셸을 거치지 않음).
  파일을 열거나 실행하지 않는다.
- 반복 호출 제한: `run`은 1분 안에 2회째부터, `reveal`은 10초 안에 3회째부터 무시한다.
  호출마다 새 프로세스가 뜨므로 기록은 `state.db`에 둔다. 형식 검사를 통과한 호출만 센다.
- 조건에 맞지 않으면 아무것도 하지 않고 `agent.log`에만 남긴다(경로는 남기지 않음).
"""

import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from rag_agent.logs import event, get_logger
from rag_agent.rules import SyncRules, is_under
from rag_agent.state import StateDB

SCHEME = "chanjin-sync"
RUN_SOURCES = frozenset({"web", "cli", "schedule"})
_KEY = re.compile(r"^[0-9a-fA-F]{64}$")

# (창 길이 초, 창 안에서 허용하는 횟수)
RATE_LIMITS: dict[str, tuple[float, int]] = {"run": (60.0, 1), "reveal": (10.0, 2)}

Action = Literal["run", "reveal"]
Status = Literal["ran", "revealed", "rejected", "rate_limited", "not_found"]
Launcher = Callable[[list[str]], None]


class ProtocolError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProtocolRequest:
    action: Action
    source: str | None = None
    key: str | None = None


@dataclass(frozen=True)
class ProtocolResult:
    status: Status
    action: str | None = None
    reason: str | None = None


def _single(params: dict[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    if values is None:
        return None
    if len(values) != 1:
        raise ProtocolError(f"duplicate_{name}")
    return values[0]


def parse_url(url: str) -> ProtocolRequest:
    if len(url) > 2048:
        raise ProtocolError("too_long")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as e:
        raise ProtocolError("malformed") from e
    if parts.scheme.lower() != SCHEME:
        raise ProtocolError("scheme")
    if parts.username or parts.password or port is not None:
        raise ProtocolError("malformed")
    if parts.path not in ("", "/"):
        raise ProtocolError("unexpected_path")
    action = (parts.hostname or "").lower()
    params = parse_qs(parts.query, keep_blank_values=True)
    if action == "run":
        source = _single(params, "source")
        if source is None:
            source = "web"
        if source not in RUN_SOURCES:
            raise ProtocolError("invalid_source")
        return ProtocolRequest(action="run", source=source)
    if action == "reveal":
        key = _single(params, "k")
        if key is None or not _KEY.match(key):
            raise ProtocolError("invalid_key")
        return ProtocolRequest(action="reveal", key=key.lower())
    raise ProtocolError("unknown_action")


def explorer_path() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")


def launch_explorer(args: list[str]) -> None:
    """셸 없이 인자 배열로 실행한다. 결과를 기다리지 않는다."""
    subprocess.Popen(args, shell=False, close_fds=True)  # noqa: S603


class ProtocolHandler:
    def __init__(
        self,
        state: StateDB,
        run_sync: Callable[[str], object],
        launcher: Launcher = launch_explorer,
        clock: Callable[[], float] = time.time,
        logger: logging.Logger | None = None,
    ) -> None:
        self.state = state
        self.run_sync = run_sync
        self.launcher = launcher
        self.clock = clock
        self.log = logger or get_logger()

    def handle(self, url: str) -> ProtocolResult:
        try:
            req = parse_url(url)
        except ProtocolError as e:
            return self._done(ProtocolResult(status="rejected", reason=e.reason))
        window, max_calls = RATE_LIMITS[req.action]
        if not self.state.record_call_if_allowed(req.action, window, max_calls, self.clock()):
            return self._done(ProtocolResult(status="rate_limited", action=req.action))
        if req.action == "run":
            assert req.source is not None
            self.run_sync(req.source)
            return self._done(ProtocolResult(status="ran", action="run"))
        assert req.key is not None
        return self._done(self._reveal(req.key))

    def _reveal(self, key: str) -> ProtocolResult:
        cached = self.state.find_by_reveal_key(key)
        if cached is None:
            return ProtocolResult(status="not_found", action="reveal", reason="unknown_key")
        config = self.state.get("config")
        if not isinstance(config, dict):
            return ProtocolResult(status="rejected", action="reveal", reason="no_config")
        rules = SyncRules.from_config(config)
        if not rules.file_allowed(cached.path):
            return ProtocolResult(status="rejected", action="reveal", reason="out_of_scope")
        fs_path = os.path.normpath(cached.fs_path)
        if not os.path.isfile(fs_path):
            return ProtocolResult(status="not_found", action="reveal", reason="missing")
        # 루트 안 경로가 링크로 루트 밖 파일을 가리키는 경우도 거절한다
        real_root = os.path.realpath(rules.allowed_root)
        if not is_under(os.path.realpath(fs_path), real_root):
            return ProtocolResult(status="rejected", action="reveal", reason="out_of_scope")
        self.launcher([explorer_path(), f"/select,{fs_path}"])
        return ProtocolResult(status="revealed", action="reveal")

    def _done(self, result: ProtocolResult) -> ProtocolResult:
        level = logging.INFO if result.status in ("ran", "revealed") else logging.WARNING
        event(
            self.log,
            f"agent.protocol.{result.status}",
            level,
            action=result.action,
            reason=result.reason,
        )
        return result
