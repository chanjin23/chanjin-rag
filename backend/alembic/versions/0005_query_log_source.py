"""add source to query_logs (TRD 5.3, 14장 평가 CLI).

평가 실행(`rag-eval run`)이 남긴 질의 로그를 채팅 질의와 구분한다. 값: `chat`, `eval`.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "query_logs",
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'chat'")),
    )
    op.create_check_constraint(
        op.f("ck_query_logs_source"),
        "query_logs",
        "source IN ('chat', 'eval')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_query_logs_source"), "query_logs", type_="check")
    op.drop_column("query_logs", "source")
