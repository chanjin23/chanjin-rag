"""initial schema (TRD 5): all tables and indexes, sync_config seed.

초기 스키마: TRD 5장의 모든 테이블과 색인, sync_config 초기값.
(첫 줄은 Alembic이 콘솔에 출력하므로 ASCII로 둔다. Windows 콘솔에서 한글이 깨지지 않게.)

Revision ID: 0001
Revises:
Create Date: 2026-11-08

- 확장: pgvector(`vector`, halfvec), pg_search(BM25). ParadeDB 이미지에는 이미 있지만 없으면 만든다.
  다운그레이드는 확장을 지우지 않는다(같은 DB의 다른 객체가 쓸 수 있음).
- `chunks.embedding`은 마이그레이션을 실행할 때의 `EMBED_DIM`(기본 2048) 차원 halfvec이다.
  차원을 바꾸려면 새 마이그레이션으로 컬럼과 HNSW 색인을 다시 만들고 임베딩을 다시 받는다(TRD 6.3).
- 상태 값은 varchar + CHECK 제약(app/db/enums.py). 값 목록은 여기 그대로 고정해 둔다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from app.core.config import get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# sync_config 초기값(TRD 5.1, 6.1). 고정 제외(/project/, /backup/)는 코드에 두므로 넣지 않는다.
DEFAULT_ROOTS = [r"C:\Chanjin\dev"]
DEFAULT_EXCLUDES = [".git/", "node_modules/", ".venv/", "venv/", "__pycache__/", "~$*", "*.tmp"]
DEFAULT_MAX_FILE_MB = 10


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search")

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"], unique=False)
    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("path_key", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("ext", sa.String(length=16), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "meta_only",
                "extracting",
                "indexed",
                "no_text",
                "embedding",
                "ready",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "failure_code",
            sa.Enum(
                "encrypted",
                "corrupted",
                "timeout",
                "memory",
                "hash_mismatch",
                "parser_error",
                name="failure_code",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("content_indexed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("locally_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("needs_upload", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("embed_charged_sha256", sa.String(length=64), nullable=True),
        sa.Column("embed_model", sa.Text(), nullable=True),
        sa.Column("embed_dim", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("path_key", name=op.f("uq_documents_path_key")),
    )
    op.create_index("ix_documents_locally_deleted", "documents", ["locally_deleted"], unique=False)
    op.create_index("ix_documents_sha256", "documents", ["sha256"], unique=False)
    op.create_index("ix_documents_status", "documents", ["status"], unique=False)
    op.create_table(
        "embed_quota",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("chunks_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("day", name=op.f("pk_embed_quota")),
    )
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_runs")),
    )
    op.create_table(
        "sync_config",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("roots", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("excludes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("max_file_mb", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_sync_config_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_config")),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "schedule",
                "web",
                "cli",
                name="source",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                "abandoned",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="running",
            nullable=False,
        ),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index("ix_sync_runs_started_at", "sync_runs", ["started_at"], unique=False)
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("failed_logins", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "location",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "heading_path",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("bm25_text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", HALFVEC(dim=get_settings().embed_dim), nullable=True),
        sa.Column("embed_model", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint("document_id", "seq", name=op.f("uq_chunks_document_id_seq")),
    )
    op.create_index("ix_chunks_content_sha256", "chunks", ["content_sha256"], unique=False)
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
    )
    op.create_table(
        "eval_results",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column(
            "retrieved",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column(
            "scores",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["eval_runs.id"],
            name=op.f("fk_eval_results_run_id_eval_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "item_id", name=op.f("pk_eval_results")),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "extract",
                "index",
                "embed",
                name="type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "done",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("priority", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index("ix_jobs_document_id", "jobs", ["document_id"], unique=False)
    op.create_index(
        "ix_jobs_queue",
        "jobs",
        ["status", "type", sa.literal_column("priority DESC")],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "user",
                "assistant",
                name="role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "streaming",
                "complete",
                "aborted",
                "error",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="complete",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(
        "ix_messages_conversation_id", "messages", ["conversation_id", "id"], unique=False
    )
    op.create_table(
        "sections",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "location",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "heading_path",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "text",
                "table",
                "slide",
                name="kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_sections_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sections")),
        sa.UniqueConstraint("document_id", "seq", name=op.f("uq_sections_document_id_seq")),
    )
    op.create_table(
        "sessions",
        sa.Column("id_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_sessions")),
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                "wrong",
                "not_found_wrong",
                "other",
                name="reason",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "failure_type",
            sa.Enum(
                "parse",
                "chunk",
                "retrieval",
                "rerank",
                "hallucination",
                name="failure_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("rating IN (-1, 1)", name=op.f("ck_feedback_rating")),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_feedback_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
        sa.UniqueConstraint("message_id", name=op.f("uq_feedback_message_id")),
    )
    op.create_table(
        "query_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("rewritten_query", sa.Text(), nullable=True),
        sa.Column("keyword_query", sa.Text(), nullable=True),
        sa.Column(
            "filters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "retrieved",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("grade", sa.Text(), nullable=True),
        sa.Column("retried", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("not_found", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "coverage",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "latency_ms",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "models",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_query_logs_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_query_logs")),
    )
    op.create_index("ix_query_logs_message_id", "query_logs", ["message_id"], unique=False)

    # BM25(pg_search 0.26): 첫 컬럼이 키. bm25_text는 Kiwi 토큰이라 공백으로만 자르고(TRD 6.4),
    # 파일명·제목은 구두점·밑줄에서 자르고 소문자로 맞추는 simple 토크나이저로 색인(TRD 5.4).
    op.execute(
        "CREATE INDEX ix_chunks_bm25 ON chunks USING bm25 ("
        "id, (bm25_text::pdb.whitespace), (filename::pdb.simple), (heading_path::pdb.simple))"
    )

    sync_config = sa.table(
        "sync_config",
        sa.column("id", sa.SmallInteger),
        sa.column("roots", JSONB),
        sa.column("excludes", JSONB),
        sa.column("max_file_mb", sa.Integer),
    )
    op.bulk_insert(
        sync_config,
        [
            {
                "id": 1,
                "roots": DEFAULT_ROOTS,
                "excludes": DEFAULT_EXCLUDES,
                "max_file_mb": DEFAULT_MAX_FILE_MB,
            }
        ],
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_bm25")
    op.drop_index("ix_query_logs_message_id", table_name="query_logs")
    op.drop_table("query_logs")
    op.drop_table("feedback")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("sections")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_jobs_queue", table_name="jobs")
    op.drop_index("ix_jobs_document_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("eval_results")
    op.drop_index(
        "ix_chunks_embedding_hnsw",
        table_name="chunks",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
    )
    op.drop_index("ix_chunks_content_sha256", table_name="chunks")
    op.drop_table("chunks")
    op.drop_table("users")
    op.drop_index("ix_sync_runs_started_at", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_table("sync_config")
    op.drop_table("eval_runs")
    op.drop_table("embed_quota")
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_index("ix_documents_sha256", table_name="documents")
    op.drop_index("ix_documents_locally_deleted", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_conversations_updated_at", table_name="conversations")
    op.drop_table("conversations")
    op.drop_table("api_tokens")
