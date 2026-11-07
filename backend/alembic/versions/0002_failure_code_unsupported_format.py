"""add unsupported_format failure code (TRD 3.4).

실패 사유 코드에 `unsupported_format`(HWP·HWPX·XLSX, 후속 T18에서 지원)을 추가한다.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08

- 값 목록은 varchar + CHECK 제약이므로 제약만 다시 만든다(app/db/enums.py).
- 다운그레이드는 이 코드로 기록된 문서를 `parser_error`로 바꾼 뒤 제약을 되돌린다.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASE_CODES = ("encrypted", "corrupted", "timeout", "memory", "hash_mismatch", "parser_error")
_NEW_CODES = (*_BASE_CODES, "unsupported_format")


def _check(codes: Sequence[str]) -> str:
    values = ", ".join(f"'{c}'" for c in codes)
    return f"failure_code IN ({values})"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_documents_failure_code"), "documents", type_="check")
    op.create_check_constraint(op.f("ck_documents_failure_code"), "documents", _check(_NEW_CODES))


def downgrade() -> None:
    op.execute(
        "UPDATE documents SET failure_code = 'parser_error' "
        "WHERE failure_code = 'unsupported_format'"
    )
    op.drop_constraint(op.f("ck_documents_failure_code"), "documents", type_="check")
    op.create_check_constraint(op.f("ck_documents_failure_code"), "documents", _check(_BASE_CODES))
