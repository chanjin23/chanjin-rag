"""async 엔진과 세션 팩토리 (TRD 2장: SQLAlchemy 2.0 async + psycopg 3).

- URL은 `DATABASE_URL`(`postgresql+psycopg://…`). 같은 URL을 동기(Alembic)·비동기 엔진이 함께 쓴다.
- psycopg의 async 모드는 Windows의 ProactorEventLoop에서 동작하지 않는다.
  Windows에서는 SelectorEventLoop로 실행해야 한다(pytest는 conftest에서 정책을 바꾼다).
"""

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def create_engine(url: str | None = None, **kwargs: Any) -> AsyncEngine:
    """async 엔진을 만든다. url이 없으면 설정의 `DATABASE_URL`을 쓴다."""
    kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(url or get_settings().database_url, **kwargs)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # commit 뒤에도 객체 속성을 읽을 수 있게 expire_on_commit=False
    return async_sessionmaker(engine, expire_on_commit=False)


@lru_cache
def get_engine() -> AsyncEngine:
    """앱 전체가 공유하는 엔진(프로세스당 1개)."""
    return create_engine()


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(get_engine())


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 의존성: 요청 하나에 세션 하나. commit은 호출하는 쪽이 한다."""
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    """앱 종료 시 연결 풀을 닫는다."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()
