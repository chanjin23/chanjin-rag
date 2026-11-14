"""Alembic 마이그레이션 (TRD 3.2 `migrate`, 12.3).

테스트 DB를 직접 내렸다 올리므로, 끝날 때는 항상 head 상태로 되돌려 다른 테스트에 영향이 없게 한다.
"""

from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text

from app.db import migrate
from app.db.base import Base

# TRD 5장의 테이블 전부
APP_TABLES = {
    "sync_config",
    "documents",
    "sections",
    "chunks",
    "jobs",
    "embed_quota",
    "sync_runs",
    "worker_heartbeats",
    "provider_status",
    "users",
    "sessions",
    "api_tokens",
    "conversations",
    "messages",
    "query_logs",
    "feedback",
    "eval_runs",
    "eval_results",
}


@pytest.fixture
def sync_engine(migrated_db: str) -> Iterator[Engine]:
    engine = create_engine(migrated_db)
    try:
        yield engine
    finally:
        engine.dispose()
        # 실패해도 다음 테스트가 쓸 수 있게 최신 스키마로 되돌린다
        migrate.upgrade("head", url=migrated_db)


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def test_upgrade_then_downgrade(migrated_db: str, sync_engine: Engine) -> None:
    # 세션 픽스처가 head까지 올려 둔 상태
    assert APP_TABLES <= _tables(sync_engine)
    assert _revision(sync_engine) == "0006"

    migrate.downgrade("base", url=migrated_db)
    assert _tables(sync_engine) & APP_TABLES == set()
    assert _revision(sync_engine) is None
    with sync_engine.connect() as conn:
        # 확장은 지우지 않는다(같은 DB의 다른 객체가 쓸 수 있음)
        exts = conn.execute(
            text("select extname from pg_extension where extname in ('vector', 'pg_search')")
        ).scalars()
        assert set(exts) == {"vector", "pg_search"}

    migrate.upgrade("head", url=migrated_db)
    assert APP_TABLES <= _tables(sync_engine)
    assert _revision(sync_engine) == "0006"
    with sync_engine.connect() as conn:
        # 다시 올려도 초기값은 한 행만 있다
        assert conn.execute(text("select count(*) from sync_config")).scalar_one() == 1


def test_models_match_migration(sync_engine: Engine) -> None:
    """ORM 모델과 마이그레이션 결과 스키마가 같아야 한다(마이그레이션 누락 방지)."""
    with sync_engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={
                "include_name": migrate.include_name,
                "include_object": migrate.include_object,
                "compare_type": True,
                "compare_server_default": True,
            },
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []
