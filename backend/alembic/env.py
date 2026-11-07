"""Alembic 실행 환경.

- DB URL: Config의 `sqlalchemy.url`(테스트·`rag-backend migrate`가 넣음), 없으면 `DATABASE_URL`.
- 마이그레이션은 동기 엔진(psycopg 3)으로 실행한다. 앱은 같은 URL로 async 엔진을 쓴다.
  동기로 돌리면 Windows의 이벤트 루프 제약(psycopg async)과 무관하게 어디서나 실행된다.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.db import models  # noqa: F401  (모든 테이블을 metadata에 등록)
from app.db.base import Base
from app.db.migrate import include_name, include_object

config = context.config

if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
