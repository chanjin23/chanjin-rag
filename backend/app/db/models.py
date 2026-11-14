"""ORM 모델 (TRD 5장). 상세 DDL의 기준은 `backend/alembic/versions/`의 마이그레이션이다.

모델을 바꾸면 새 마이그레이션을 추가한다(`test_migrations.py`가 모델과 마이그레이션의 차이를 검사).
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.core.config import get_settings
from app.db.base import Base
from app.db.enums import (
    DocumentStatus,
    FailureCode,
    FailureType,
    FeedbackReason,
    JobStatus,
    JobType,
    MessageRole,
    MessageStatus,
    SectionKind,
    SyncRunSource,
    SyncRunStatus,
)
from app.db.keys import make_path_key

# 임베딩 차원(TRD 6.3, 12.1 `EMBED_DIM`). 마이그레이션도 같은 설정값으로 컬럼을 만든다.
EMBED_DIM = get_settings().embed_dim

EMPTY_JSON_OBJECT = text("'{}'::jsonb")
EMPTY_JSON_ARRAY = text("'[]'::jsonb")
EMPTY_TEXT_ARRAY = text("'{}'::text[]")


def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """문자열 + CHECK 제약으로 저장하는 Enum 타입. 제약 이름은 `ck_<테이블>_<name>`."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def timestamptz(*, nullable: bool = True, now: bool = False, onupdate: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if now else None,
        onupdate=func.now() if onupdate else None,
    )


def bigint_pk() -> Any:
    return mapped_column(BigInteger, Identity(always=False), primary_key=True)


def flag() -> Any:
    """기본값 false인 boolean."""
    return mapped_column(Boolean, nullable=False, server_default=text("false"))


def counter() -> Any:
    """기본값 0인 정수."""
    return mapped_column(Integer, nullable=False, server_default=text("0"))


def jsonb(default: Any) -> Any:
    return mapped_column(JSONB, nullable=False, server_default=default)


def document_fk() -> Any:
    return mapped_column(BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)


# ---------------------------------------------------------------------------
# 5.1 수집
# ---------------------------------------------------------------------------


class SyncConfig(Base):
    """동기화 설정. 단일 행(id=1). 고정 제외(`/project/`, `/backup/`)는 코드에 둔다."""

    __tablename__ = "sync_config"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("1"))
    roots: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    excludes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # 웹에서 바꾸지 않는다(Nginx 업로드 상한과 함께 바꿔야 하므로)
    max_file_mb: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    # 대량 삭제 보호로 보류된 판정의 적용 승인(동기화 화면). 다음 manifest 판정이 쓰고 비운다.
    held_deletions_approved_at: Mapped[datetime | None] = timestamptz()
    held_deletions_run_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = timestamptz(nullable=False, now=True, onupdate=True)


class Document(Base):
    """파일 1개 = 1행. `path`는 원래 Windows 경로, `path_key`는 NFC + 소문자(unique)."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("path_key"),
        Index("ix_documents_sha256", "sha256"),
        Index("ix_documents_status", "status"),
        Index("ix_documents_locally_deleted", "locally_deleted"),
    )

    id: Mapped[int] = bigint_pk()
    path: Mapped[str] = mapped_column(Text, nullable=False)
    path_key: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    ext: Mapped[str] = mapped_column(String(16), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime: Mapped[datetime] = timestamptz(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        str_enum(DocumentStatus, "status"),
        nullable=False,
        server_default=DocumentStatus.QUEUED.value,
    )
    failure_code: Mapped[FailureCode | None] = mapped_column(
        str_enum(FailureCode, "failure_code"), nullable=True
    )
    failure_detail: Mapped[str | None] = mapped_column(Text)
    content_indexed: Mapped[bool] = flag()
    locally_deleted: Mapped[bool] = flag()
    deleted_at: Mapped[datetime | None] = timestamptz()
    needs_upload: Mapped[bool] = flag()
    # 하루 임베딩 한도를 이미 차감한 버전(TRD 6.2)
    embed_charged_sha256: Mapped[str | None] = mapped_column(String(64))
    embed_model: Mapped[str | None] = mapped_column(Text)
    embed_dim: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    updated_at: Mapped[datetime] = timestamptz(nullable=False, now=True, onupdate=True)

    sections: Mapped[list["Section"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    @validates("path_key")
    def _normalize_path_key(self, _key: str, value: str) -> str:
        return make_path_key(value)

    @validates("ext")
    def _lower_ext(self, _key: str, value: str) -> str:
        return value.lower()


class Section(Base):
    """추출 텍스트 원본. 재청킹은 여기서 다시 하므로 재업로드가 필요 없다."""

    __tablename__ = "sections"
    __table_args__ = (UniqueConstraint("document_id", "seq"),)

    id: Mapped[int] = bigint_pk()
    document_id: Mapped[int] = document_fk()
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    location: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)
    heading_path: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=EMPTY_TEXT_ARRAY
    )
    kind: Mapped[SectionKind] = mapped_column(str_enum(SectionKind, "kind"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    document: Mapped[Document] = relationship(back_populates="sections")


class Chunk(Base):
    """검색 단위. `bm25_text`는 Kiwi 토큰을 공백으로 이은 문자열(TRD 6.4).

    `filename`은 `documents.filename`의 사본이다. pg_search BM25 색인은 한 테이블의 컬럼만
    색인할 수 있어서, 파일명으로도 검색되도록 청크에 복사해 둔다(파일 이동 시 함께 갱신).
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "seq"),
        Index("ix_chunks_content_sha256", "content_sha256"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
        # pg_search 0.26 문법: 첫 컬럼이 키(key_field는 0.26부터 쓰지 않음).
        # bm25_text는 Kiwi가 이미 토큰화했으므로 공백으로만 자르고,
        # 파일명·제목은 구두점·밑줄에서 자르고 소문자로 맞추는 simple 토크나이저를 쓴다.
        Index(
            "ix_chunks_bm25",
            "id",
            text("(bm25_text::pdb.whitespace)"),
            text("(filename::pdb.simple)"),
            text("(heading_path::pdb.simple)"),
            postgresql_using="bm25",
        ),
    )

    id: Mapped[int] = bigint_pk()
    document_id: Mapped[int] = document_fk()
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    location: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)
    heading_path: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=EMPTY_TEXT_ARRAY
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 임베딩 입력(머리말 포함)의 해시. 같은 문서의 이전 청크 임베딩을 재사용할 때 쓴다(TRD 6.2)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bm25_text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[Any | None] = mapped_column(HALFVEC(EMBED_DIM), nullable=True)
    embed_model: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="chunks")


class Job(Base):
    """작업 큐(`SELECT … FOR UPDATE SKIP LOCKED`). `priority`는 파일 수정 시각(최근 우선)."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_queue", "status", "type", text("priority DESC")),
        Index("ix_jobs_document_id", "document_id"),
    )

    id: Mapped[int] = bigint_pk()
    type: Mapped[JobType] = mapped_column(str_enum(JobType, "type"), nullable=False)
    document_id: Mapped[int] = document_fk()
    # 작업 대상 버전. 끝날 때 문서의 현재 sha256과 다르면 결과를 버린다
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "status"), nullable=False, server_default=JobStatus.QUEUED.value
    )
    priority: Mapped[datetime] = timestamptz(nullable=False)
    attempts: Mapped[int] = counter()
    run_after: Mapped[datetime] = timestamptz(nullable=False, now=True)
    locked_at: Mapped[datetime | None] = timestamptz()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    updated_at: Mapped[datetime] = timestamptz(nullable=False, now=True, onupdate=True)


class EmbedQuota(Base):
    """한국 시간 날짜별 임베딩 사용량(파일 수 `used`, 청크 수 `chunks_used`)."""

    __tablename__ = "embed_quota"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    used: Mapped[int] = counter()
    chunks_used: Mapped[int] = counter()


class SyncRun(Base):
    """동기화 실행 이력. `heartbeat_at`이 끊기면 `abandoned`로 바꾼다(`SYNC_RUN_STALE_MINUTES`)."""

    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_started_at", "started_at"),)

    id: Mapped[int] = bigint_pk()
    source: Mapped[SyncRunSource] = mapped_column(str_enum(SyncRunSource, "source"), nullable=False)
    started_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    finished_at: Mapped[datetime | None] = timestamptz()
    heartbeat_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    status: Mapped[SyncRunStatus] = mapped_column(
        str_enum(SyncRunStatus, "status"),
        nullable=False,
        server_default=SyncRunStatus.RUNNING.value,
    )
    stats: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)


class WorkerHeartbeat(Base):
    """Worker 하트비트(TRD 12.4). Worker가 30초마다 갱신하고 `/health`가 읽는다.

    Worker 이름(기본 `worker`)마다 1행. 시작 시각과 프로세스 정보를 함께 남긴다.
    """

    __tablename__ = "worker_heartbeats"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    heartbeat_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    pid: Mapped[int | None] = mapped_column(Integer)
    info: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)


class ProviderStatus(Base):
    """모델 호출 종류(`llm`·`embed`·`rerank`)마다 마지막 성공·실패 시각과 오류 코드(TRD 12.4).

    API와 Worker가 각자 기록하고 `/health`가 읽는다(`app.providers.monitor`).
    `last_error_code`는 TRD 7.3 오류 코드(`provider_auth`, `rate_limited` 등).
    """

    __tablename__ = "provider_status"

    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[datetime | None] = timestamptz()
    last_failure_at: Mapped[datetime | None] = timestamptz()
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    # 같은 오류 코드로 이어진 연속 실패의 첫 시각. 성공하면 NULL.
    # `/health`의 `since`(배너가 실패마다 다시 뜨지 않게)
    failing_since: Mapped[datetime | None] = timestamptz()
    updated_at: Mapped[datetime] = timestamptz(nullable=False, now=True)


# ---------------------------------------------------------------------------
# 5.2 인증
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username"),)

    id: Mapped[int] = bigint_pk()
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    failed_logins: Mapped[int] = counter()
    locked_until: Mapped[datetime | None] = timestamptz()
    created_at: Mapped[datetime] = timestamptz(nullable=False, now=True)


class Session(Base):
    """로그인 세션. 쿠키 값의 SHA-256(16진수 64자)만 저장한다."""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = timestamptz(nullable=False)
    last_seen_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    created_at: Mapped[datetime] = timestamptz(nullable=False, now=True)


class ApiToken(Base):
    """Sync Agent용 Bearer 토큰. 토큰의 SHA-256만 저장한다."""

    __tablename__ = "api_tokens"
    __table_args__ = (UniqueConstraint("token_hash"),)

    id: Mapped[int] = bigint_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    last_used_at: Mapped[datetime | None] = timestamptz()
    # Agent 첫 접속 시각(첫 실행 체크리스트, TRD 8.3 `/setup/status`)
    first_used_at: Mapped[datetime | None] = timestamptz()
    revoked_at: Mapped[datetime | None] = timestamptz()


# ---------------------------------------------------------------------------
# 5.3 대화, 로그, 평가
# ---------------------------------------------------------------------------


class Conversation(Base):
    """대화(F-11). 지우면 메시지, 질의 로그, 피드백도 함께 지워진다(ON DELETE CASCADE)."""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_updated_at", "updated_at"),)

    id: Mapped[int] = bigint_pk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    updated_at: Mapped[datetime] = timestamptz(nullable=False, now=True, onupdate=True)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.id",
    )


class Message(Base):
    """대화 메시지. `citations`는 저장 시점 값으로 화면을 그린다(청크가 바뀌어도 인용 유지)."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_id", "conversation_id", "id"),)

    id: Mapped[int] = bigint_pk()
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(str_enum(MessageRole, "role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    citations: Mapped[list[dict[str, Any]]] = jsonb(EMPTY_JSON_ARRAY)
    status: Mapped[MessageStatus] = mapped_column(
        str_enum(MessageStatus, "status"),
        nullable=False,
        server_default=MessageStatus.COMPLETE.value,
    )
    created_at: Mapped[datetime] = timestamptz(nullable=False, now=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class QueryLog(Base):
    """질의 로그. `retrieved`에는 청크 ID와 점수만 둔다(문서를 지워도 그대로 둠, TRD 6.5).

    `message_id`는 NULL을 허용한다(대화 없이 실행하는 평가 질의 등).
    `source`: `chat`(채팅 질문), `eval`(`rag-eval run` 평가 질문).
    """

    __tablename__ = "query_logs"
    __table_args__ = (
        Index("ix_query_logs_message_id", "message_id"),
        CheckConstraint("source IN ('chat', 'eval')", name="source"),
    )

    id: Mapped[int] = bigint_pk()
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
    rewritten_query: Mapped[str | None] = mapped_column(Text)
    keyword_query: Mapped[str | None] = mapped_column(Text)
    filters: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)
    retrieved: Mapped[list[dict[str, Any]]] = jsonb(EMPTY_JSON_ARRAY)
    grade: Mapped[str | None] = mapped_column(Text)
    retried: Mapped[bool] = flag()
    not_found: Mapped[bool] = flag()
    coverage: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)
    latency_ms: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)
    models: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'chat'"))
    created_at: Mapped[datetime] = timestamptz(nullable=False, now=True)


class Feedback(Base):
    """답변 피드백(F-12). 답변당 1개이며 다시 누르면 바뀐다."""

    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("message_id"),
        CheckConstraint("rating IN (-1, 1)", name="rating"),
    )

    id: Mapped[int] = bigint_pk()
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    reason: Mapped[FeedbackReason | None] = mapped_column(
        str_enum(FeedbackReason, "reason"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text)
    failure_type: Mapped[FailureType | None] = mapped_column(
        str_enum(FailureType, "failure_type"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = timestamptz()
    created_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    updated_at: Mapped[datetime] = timestamptz(nullable=False, now=True, onupdate=True)


class EvalRun(Base):
    """평가 실행(F-13). `config`에 판정 모델 이름 등을 기록한다."""

    __tablename__ = "eval_runs"

    id: Mapped[int] = bigint_pk()
    dataset_version: Mapped[str] = mapped_column(Text, nullable=False)
    git_sha: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)
    metrics: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)
    started_at: Mapped[datetime] = timestamptz(nullable=False, now=True)
    finished_at: Mapped[datetime | None] = timestamptz()


class EvalResult(Base):
    """문항별 평가 결과. `item_id`는 평가셋 YAML의 문항 ID."""

    __tablename__ = "eval_results"
    __table_args__ = (PrimaryKeyConstraint("run_id", "item_id"),)

    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved: Mapped[list[dict[str, Any]]] = jsonb(EMPTY_JSON_ARRAY)
    answer: Mapped[str | None] = mapped_column(Text)
    scores: Mapped[dict[str, Any]] = jsonb(EMPTY_JSON_OBJECT)
