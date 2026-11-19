"""provider_status, api_tokens.first_used_at (TRD 5.1·5.2, 8.3 `/health`·`/setup/status`, 12.4).

- `provider_status`: 모델 호출 종류(`llm`·`embed`·`rerank`)마다 마지막 성공·실패 시각과 오류 코드.
  API와 Worker가 서로 다른 프로세스라 DB에 남겨 `/health`가 함께 읽는다.
  `failing_since`는 같은 오류 코드로 이어진 연속 실패의 첫 시각(성공하면 비움). 배너가 실패마다
  다시 뜨지 않게 이 시각을 `since`로 쓴다.
- `api_tokens.first_used_at`: Agent 첫 접속 시각(첫 실행 체크리스트).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_status",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("kind", name=op.f("pk_provider_status")),
    )
    op.add_column(
        "api_tokens", sa.Column("first_used_at", sa.DateTime(timezone=True), nullable=True)
    )
    # 이미 쓰인 토큰은 마지막 사용 시각을 첫 접속 시각으로 본다(그보다 이른 기록이 없다)
    op.execute("UPDATE api_tokens SET first_used_at = last_used_at WHERE last_used_at IS NOT NULL")


def downgrade() -> None:
    op.drop_column("api_tokens", "first_used_at")
    op.drop_table("provider_status")
