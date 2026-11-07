"""Alembic 실행 (TRD 3.2 `migrate` 서비스, 12.3).

`rag-backend migrate`가 이 함수를 부른다. `backend/`에서 `uv run alembic …`으로 직접 실행해도 된다.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.schema import SchemaItem

from app.core.config import get_settings
from app.core.logging import get_logger

# backend/app/db/migrate.py → backend/
BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

# autogenerate가 비교하지 못하는 pg_search 식 색인은 비교에서 빼고 마이그레이션에 직접 적는다.
UNCOMPARED_INDEXES = frozenset({"ix_chunks_bm25"})


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    """비교할 때 모델에 없는 테이블은 반영(reflect)조차 하지 않는다.

    ParadeDB 이미지의 PostGIS 테이블(`spatial_ref_sys` 등)을 반영하면 모르는 타입 경고가 난다.
    """
    if type_ == "table":
        from app.db import models  # noqa: F401  (모든 테이블을 metadata에 등록)
        from app.db.base import Base

        return name in Base.metadata.tables
    return True


def include_object(
    obj: SchemaItem, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """autogenerate·`alembic check`·테스트의 모델 비교 대상 필터."""
    # ParadeDB 이미지가 만든 확장 테이블(PostGIS의 spatial_ref_sys 등)은 모델에 없으므로 무시한다
    if type_ == "table" and reflected and compare_to is None:
        return False
    if type_ == "index" and name in UNCOMPARED_INDEXES:
        return False
    return True


def alembic_config(url: str | None = None) -> Config:
    """Alembic 설정. url이 없으면 설정의 `DATABASE_URL`을 쓴다(env.py가 읽음)."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # ini 보간 문법(%)과 겹치지 않게 %를 두 번 쓴다
    cfg.set_main_option("sqlalchemy.url", (url or get_settings().database_url).replace("%", "%%"))
    return cfg


def _safe_url(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


def upgrade(revision: str = "head", url: str | None = None) -> None:
    cfg = alembic_config(url)
    log = get_logger("app.db")
    target = _safe_url(cfg.get_main_option("sqlalchemy.url") or "")
    log.info("db.migrate.started", direction="upgrade", revision=revision, url=target)
    command.upgrade(cfg, revision)
    log.info("db.migrate.completed", direction="upgrade", revision=revision, url=target)


def downgrade(revision: str, url: str | None = None) -> None:
    cfg = alembic_config(url)
    log = get_logger("app.db")
    target = _safe_url(cfg.get_main_option("sqlalchemy.url") or "")
    log.info("db.migrate.started", direction="downgrade", revision=revision, url=target)
    command.downgrade(cfg, revision)
    log.info("db.migrate.completed", direction="downgrade", revision=revision, url=target)
