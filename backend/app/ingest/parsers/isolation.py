"""파서 격리 실행 (TRD 3.4, 11장): 하위 프로세스에서 타임아웃·메모리 상한과 함께 파싱한다.

- 자식: `python -m app.ingest.parsers._child`. 메모리 상한은 자식이 시작하자마자 스스로 건다
  (`memlimit.apply_memory_limit`: Windows Job Object, POSIX RLIMIT_AS).
- 타임아웃(기본 `PARSER_TIMEOUT_SECONDS`=60초)을 넘기면 자식을 죽이고 `timeout`.
- 자식이 결과 없이 죽으면: `MemoryError` 흔적이 있거나 SIGKILL(리눅스 OOM)이면 `memory`,
  그 밖에는 `parser_error`. 어떤 경우에도 호출한 프로세스(Worker)는 계속 돈다.
- 지원하지 않는 형식은 자식을 띄우지 않고 바로 `unsupported_format`.
- 로그: `parse.completed`(성공·no_text), `parse.failed`(실패). 파일 경로·내용은 남기지 않는다.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import FailureCode
from app.ingest.parsers.base import RESULT_PREFIX, WARNING_SCANNED_PAGE, ParseResult
from app.ingest.parsers.registry import is_supported, normalize_extension, unsupported_result

log = get_logger(__name__)

_STDERR_TAIL = 400


def _decode_result(stdout: bytes) -> ParseResult | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                data: dict[str, Any] = json.loads(line[len(RESULT_PREFIX) :].decode("utf-8"))
                return ParseResult.from_dict(data)
            except (ValueError, KeyError):  # 결과 줄이 잘림 등. 자식 비정상 종료로 처리
                return None
    return None


def _crash_result(returncode: int, stderr: bytes) -> ParseResult:
    tail = stderr.decode("utf-8", errors="replace").strip()[-_STDERR_TAIL:]
    killed = sys.platform != "win32" and returncode == -signal.SIGKILL
    if "MemoryError" in tail or "out of memory" in tail.lower() or killed:
        return ParseResult.failed(FailureCode.MEMORY, f"자식 프로세스 종료(코드 {returncode})")
    detail = f"자식 프로세스 종료(코드 {returncode})"
    if tail:
        detail += f": {tail.splitlines()[-1]}"
    return ParseResult.failed(FailureCode.PARSER_ERROR, detail)


def parse_in_subprocess(
    path: Path,
    *,
    timeout: float | None = None,
    memory_limit_mb: int | None = None,
    parser_ref: str | None = None,
    **log_fields: Any,
) -> ParseResult:
    """`path`를 하위 프로세스에서 파싱한다.

    `parser_ref`(`모듈:이름`)를 주면 등록표 대신 그 파서를 쓴다(테스트용).
    `log_fields`는 로그에 함께 남길 값(예: `document_id`)이다.
    """
    settings = get_settings()
    timeout = settings.parser_timeout_seconds if timeout is None else timeout
    memory_mb = settings.parser_memory_limit_mb if memory_limit_mb is None else memory_limit_mb
    ext = normalize_extension(path)
    started = time.monotonic()

    if parser_ref is None and not is_supported(ext):
        result = unsupported_result(ext)
        _log(result, ext=ext, started=started, isolated=False, **log_fields)
        return result

    cmd = [sys.executable, "-m", "app.ingest.parsers._child", "--memory-mb", str(memory_mb)]
    if parser_ref:
        cmd += ["--parser", parser_ref]
    cmd += ["--", str(path)]  # 경로가 `-`로 시작해도 옵션으로 읽히지 않게
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=creationflags,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        result = ParseResult.failed(FailureCode.TIMEOUT, f"파싱 {timeout:g}초 초과")
    else:
        decoded = _decode_result(stdout)
        result = decoded if decoded is not None else _crash_result(proc.returncode, stderr)

    limit_error = result.meta.pop("memory_limit_error", None)
    if limit_error:
        log.warning("parse.memory_limit_unavailable", reason=limit_error)
    _log(result, ext=ext, started=started, isolated=True, **log_fields)
    return result


def _log(result: ParseResult, *, ext: str, started: float, isolated: bool, **fields: Any) -> None:
    elapsed_ms = round((time.monotonic() - started) * 1000)
    common: dict[str, Any] = {
        "ext": ext,
        "status": result.status,
        "elapsed_ms": elapsed_ms,
        "isolated": isolated,
        **fields,
    }
    if result.status == "failed":
        log.warning(
            "parse.failed",
            failure_code=result.failure_code.value if result.failure_code else None,
            **common,
        )
        return
    scanned = sum(1 for w in result.warnings if w.code == WARNING_SCANNED_PAGE)
    log.info(
        "parse.completed",
        sections=len(result.sections),
        chars=result.text_chars,
        warnings=len(result.warnings),
        scanned_pages=scanned,
        **common,
    )
