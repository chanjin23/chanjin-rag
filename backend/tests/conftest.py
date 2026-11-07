"""pytest 공용 설정과 픽스처 (TRD 14장).

- 모든 테스트는 `APP_ENV=test`, `PROVIDER=fake`로 실행한다. 이때 설정은 저장소 `.env`를 읽지 않고
  NVIDIA 키도 비운다(`app.core.config`). 앱 모듈을 import하기 전에 정해야 하므로 맨 위에서 설정한다.
- 테스트 DB: `TEST_DATABASE_URL`(기본 127.0.0.1:5433의 ParadeDB). `scripts/verify.sh`가
  `deploy/compose.test.yaml`로 띄운다. 테스트마다 트랜잭션을 열고 끝나면 롤백한다.
- 샘플 문서: `SAMPLE_DOCS_DIRS`(os.pathsep, Windows는 `;`로 구분). `samples` 마커 테스트만 쓴다.
"""

import os

os.environ["APP_ENV"] = "test"
os.environ["PROVIDER"] = "fake"

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402

from app.core.config import get_settings  # noqa: E402

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


@pytest.fixture
def db(
    db_connection: psycopg.Connection[tuple[object, ...]],
) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """테스트 하나의 DB 연결. 안에서 commit해도 끝나면 모두 롤백된다."""
    with db_connection.transaction(force_rollback=True):
        yield db_connection


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
