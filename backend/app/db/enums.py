"""상태·종류 값 (TRD 5장). DB에는 문자열(varchar + CHECK 제약)로 저장한다.

PostgreSQL 네이티브 enum 대신 문자열 + CHECK를 쓰는 이유: 값을 추가할 때 `ALTER TYPE` 없이
제약만 바꾸면 되고, 다운그레이드에서 타입을 따로 지울 필요가 없다.
값을 바꾸면 새 Alembic 마이그레이션으로 CHECK 제약도 함께 바꾼다.
"""

from enum import StrEnum


class DocumentStatus(StrEnum):
    """문서 상태 (TRD 5.1 상태도)."""

    QUEUED = "queued"
    META_ONLY = "meta_only"
    EXTRACTING = "extracting"
    INDEXED = "indexed"
    NO_TEXT = "no_text"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"


class FailureCode(StrEnum):
    """파싱 실패 사유 (TRD 3.4)."""

    ENCRYPTED = "encrypted"
    CORRUPTED = "corrupted"
    TIMEOUT = "timeout"
    MEMORY = "memory"
    HASH_MISMATCH = "hash_mismatch"
    PARSER_ERROR = "parser_error"
    # 아직 파서가 없는 형식(HWP·HWPX·XLSX, T18에서 지원). 파싱하지 않고 파일명 청크만 만든다.
    UNSUPPORTED_FORMAT = "unsupported_format"


class SectionKind(StrEnum):
    TEXT = "text"
    TABLE = "table"
    SLIDE = "slide"


class JobType(StrEnum):
    EXTRACT = "extract"
    INDEX = "index"
    EMBED = "embed"


class JobStatus(StrEnum):
    """작업 상태. `queued`(대기, `run_after` 이후 실행) → `running`(`locked_at` 갱신) →
    `done`(완료, 버전이 바뀌어 결과를 버린 경우 포함) 또는 `failed`(재시도 초과)."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class SyncRunSource(StrEnum):
    SCHEDULE = "schedule"
    WEB = "web"
    CLI = "cli"


class SyncRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    STREAMING = "streaming"
    COMPLETE = "complete"
    ABORTED = "aborted"
    ERROR = "error"


class FeedbackReason(StrEnum):
    WRONG = "wrong"
    NOT_FOUND_WRONG = "not_found_wrong"
    OTHER = "other"


class FailureType(StrEnum):
    """피드백 검토 시 분류하는 실패 유형 (F-12)."""

    PARSE = "parse"
    CHUNK = "chunk"
    RETRIEVAL = "retrieval"
    RERANK = "rerank"
    HALLUCINATION = "hallucination"
