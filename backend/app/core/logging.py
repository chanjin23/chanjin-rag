"""structlog 설정 (TRD 2장, 11장).

- 형식: dev는 사람이 읽기 쉬운 콘솔 출력, prod는 JSON 한 줄 출력. `LOG_FORMAT`으로 덮어쓸 수 있다.
- 파일: `LOG_FILE`을 주면 stdout과 같은 내용을 그 파일에도 이어 쓴다. E2E 테스트가 이 파일을 읽어
  "백엔드가 요청을 받아 로그를 남겼는지" 확인한다.
"""

import io
import logging
import sys
from pathlib import Path
from typing import TextIO

import structlog


class _Tee:
    """PrintLogger가 쓰는 write/flush만 구현한 출력 복제기."""

    def __init__(self, *streams: TextIO) -> None:
        self._streams = streams

    def write(self, s: str) -> int:
        for stream in self._streams:
            stream.write(s)
        return len(s)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def configure_logging(level: str = "INFO", json: bool = False, file: str | None = None) -> None:
    # Windows에서 출력이 파이프나 파일로 가면 stdout이 cp949가 되어 한글이 깨진다.
    # 그래서 UTF-8로 고정한다.
    if isinstance(sys.stdout, io.TextIOWrapper) and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    out: TextIO = sys.stdout
    if file:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        log_file = path.open("a", encoding="utf-8")  # 프로세스가 끝날 때까지 열어 둔다
        out = _Tee(sys.stdout, log_file)  # type: ignore[assignment]

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        # 컨테이너는 TZ=Asia/Seoul로 실행하므로 로컬 시각 = 한국 시간 (TRD 12.1)
        structlog.processors.TimeStamper(fmt="iso", utc=False),
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=out),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
