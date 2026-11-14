"""add held deletion approval to sync_config (TRD 6.1, 8.3).

대량 삭제 보호로 보류된 판정을 사용자가 동기화 화면에서 승인한 시각과 대상 실행 ID.
다음 manifest 판정이 보호를 건너뛰고 적용한 뒤 비운다.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sync_config",
        sa.Column("held_deletions_approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("sync_config", sa.Column("held_deletions_run_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_config", "held_deletions_run_id")
    op.drop_column("sync_config", "held_deletions_approved_at")
