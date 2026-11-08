"""Agent 로그 (TRD 10.1 `agent.log`, 11장 로그).

- `agent.log`에 JSON 한 줄씩 남긴다. 자정마다 바꾸고 7일치만 둔다(`TimedRotatingFileHandler`).
- 토큰·파일 본문은 기록하지 않는다. 혹시 메시지에 섞여도 `RedactFilter`가 토큰을 가린다.
- 이벤트는 `event(logger, "agent.sync.completed", run_id=1, ...)`처럼 이름과 필드로 남긴다.
"""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER_NAME = "rag_agent"
BACKUP_DAYS = 7
_REDACTED = "[REDACTED]"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, ensure_ascii=False, default=str)


class RedactFilter(logging.Filter):
    """비밀값이 메시지나 필드에 들어가면 가린다(방어용, 원래 넣지 않는다)."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        record.msg = self._clean(str(record.msg))
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            record.fields = {k: self._clean(v) for k, v in fields.items()}
        return True

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            for s in self.secrets:
                value = value.replace(s, _REDACTED)
        return value


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def event(logger: logging.Logger, name: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, name, extra={"fields": fields})


def configure(log_file: Path, secrets: list[str] | None = None) -> logging.Logger:
    """`agent.log`에 쓰도록 설정한다. 여러 번 불러도 핸들러를 하나만 둔다."""
    logger = get_logger()
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    for f in list(logger.filters):
        logger.removeFilter(f)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=BACKUP_DAYS, encoding="utf-8", delay=True
    )
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactFilter(secrets or []))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def close() -> None:
    logger = get_logger()
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
