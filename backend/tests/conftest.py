"""pytest 공용 설정과 픽스처 (TRD 14장).

- 모든 테스트는 `APP_ENV=test`, `PROVIDER=fake`로 실행한다. 이때 설정은 저장소 `.env`를 읽지 않고
  NVIDIA 키도 비운다(`app.core.config`). 앱 모듈을 import하기 전에 정해야 하므로 맨 위에서 설정한다.
- 테스트 DB: `TEST_DATABASE_URL`(기본 127.0.0.1:5433의 ParadeDB). `scripts/verify.sh`가
  `deploy/compose.test.yaml`로 띄운다. 테스트마다 트랜잭션을 열고 끝나면 롤백한다.
- 스키마: `migrated_db` 세션 픽스처가 pytest 시작 시 Alembic으로 `base`까지 내렸다가 `head`로 올린다
  (이전 실행·E2E가 남긴 데이터를 지우고, 현재 마이그레이션 코드와 같은 스키마로 맞춤).
- async: psycopg의 async 모드는 Windows의 ProactorEventLoop에서 동작하지 않아 Selector 정책을 쓴다.
- 샘플 문서: `SAMPLE_DOCS_DIRS`(os.pathsep, Windows는 `;`로 구분). `samples` 마커 테스트만 쓴다.
"""

import asyncio
import os
import sys

os.environ["APP_ENV"] = "test"
os.environ["PROVIDER"] = "fake"

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from collections.abc import AsyncIterator, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db import migrate  # noqa: E402
from app.db.engine import create_engine  # noqa: E402

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://rag:rag@127.0.0.1:5433/rag_test"


def resolve_test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL


def to_libpq_url(url: str) -> str:
    """SQLAlchemy 형식(`postgresql+psycopg://`)을 psycopg가 받는 형식으로 바꾼다."""
    scheme, sep, rest = url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}"


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """테스트가 monkeypatch로 바꾼 환경 변수가 다음 테스트의 설정에 남지 않게 한다."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def db_url() -> str:
    return resolve_test_database_url()


@pytest.fixture(scope="session")
def db_connection(db_url: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    try:
        conn = psycopg.connect(to_libpq_url(db_url), connect_timeout=5)
    except psycopg.OperationalError as e:
        pytest.fail(
            f"테스트 DB에 연결하지 못했습니다({db_url}). "
            "`docker compose -f deploy/compose.test.yaml up -d --wait`로 띄우세요.\n"
            f"{e}"
        )
    with conn:
        yield conn


@pytest.fixture(scope="session")
def migrated_db(db_url: str, db_connection: psycopg.Connection[tuple[object, ...]]) -> str:
    """테스트 DB를 빈 상태에서 최신 스키마(head)로 만든다. 세션에 한 번. DB URL을 돌려준다."""
    migrate.downgrade("base", url=db_url)
    migrate.upgrade("head", url=db_url)
    return db_url


@pytest.fixture
def db(
    migrated_db: str,
    db_connection: psycopg.Connection[tuple[object, ...]],
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """테스트 하나의 DB 연결(psycopg, 동기). 안에서 commit해도 끝나면 모두 롤백된다."""
    with db_connection.transaction(force_rollback=True):
        yield db_connection


@pytest.fixture
async def session(migrated_db: str) -> AsyncIterator[AsyncSession]:
    """테스트 하나의 SQLAlchemy async 세션. `commit()`은 savepoint가 되고 끝나면 모두 롤백된다."""
    engine = create_engine(migrated_db, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            s = AsyncSession(
                bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
            )
            try:
                yield s
            finally:
                await s.close()
                await outer.rollback()
    finally:
        await engine.dispose()


def sample_docs_dirs_from_env() -> list[Path]:
    raw = os.environ.get("SAMPLE_DOCS_DIRS", "")
    return [Path(p.strip()) for p in raw.split(os.pathsep) if p.strip()]


@pytest.fixture(scope="session")
def sample_docs_dirs() -> list[Path]:
    """실제 문서 폴더 목록. 읽기만 하고, 내용·파일명을 테스트 출력이나 저장소에 남기지 않는다."""
    dirs = sample_docs_dirs_from_env()
    if not dirs:
        pytest.fail("SAMPLE_DOCS_DIRS가 설정되지 않았습니다(`-m samples` 테스트 전용)")
    missing = [i for i, d in enumerate(dirs) if not d.is_dir()]
    if missing:
        pytest.fail(f"SAMPLE_DOCS_DIRS의 {missing}번째 폴더가 없습니다")
    return dirs


# ---------------------------------------------------------------------------
# 웹 API 클라이언트 (인증, TRD 11장)
# ---------------------------------------------------------------------------

# 웹 API는 GET이 아닌 요청에 CSRF 헤더 두 개가 필요하다. 테스트 클라이언트의 Host는 `testserver`.
CSRF_HEADERS = {"X-Requested-With": "fetch", "Origin": "https://testserver"}


@pytest.fixture
def web_client() -> Iterator[TestClient]:
    """로그인된 상태의 동기 TestClient(DB 없이 웹 API를 부를 때).

    세션 의존성을 가짜 사용자로 바꾸고 CSRF 헤더를 기본으로 붙인다. 실제 로그인·세션 흐름은
    `api_client`로 test_auth.py에서 확인한다.
    """
    from app.api.deps import require_web_user
    from app.db.models import User
    from app.main import app

    app.dependency_overrides[require_web_user] = lambda: User(id=1, username="tester")
    try:
        yield TestClient(app, base_url="https://testserver", headers=CSRF_HEADERS)
    finally:
        app.dependency_overrides.pop(require_web_user, None)


@pytest.fixture
async def api_client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """테스트 DB 세션(`session`, 끝나면 롤백)을 쓰는 async 클라이언트. 쿠키를 주고받는다.

    TestClient는 앱을 다른 이벤트 루프에서 돌려 async 세션을 공유할 수 없어 ASGITransport를 쓴다.
    https base_url이어야 Secure 쿠키를 다시 보낸다.
    """
    from app.db.engine import get_session
    from app.main import app

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
