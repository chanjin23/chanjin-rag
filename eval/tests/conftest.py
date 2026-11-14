"""rag-eval 테스트 설정. 백엔드 테스트의 DB 픽스처(`session` 등)와 합성 문서 도우미를 그대로 쓴다.

`backend/tests/conftest.py`를 다른 모듈 이름으로 읽어 픽스처를 다시 내보낸다(같은 테스트 DB,
테스트마다 롤백). `chat_seed`(합성 문서 넣기)는 `backend/tests`를 import 경로에 더해 쓴다.
"""

import importlib.util
import sys
from pathlib import Path

BACKEND_TESTS = Path(__file__).resolve().parents[2] / "backend" / "tests"
if str(BACKEND_TESTS) not in sys.path:
    sys.path.insert(0, str(BACKEND_TESTS))

_spec = importlib.util.spec_from_file_location(
    "backend_tests_conftest", BACKEND_TESTS / "conftest.py"
)
assert _spec is not None and _spec.loader is not None
_backend = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_backend)

_fresh_settings = _backend._fresh_settings
db_url = _backend.db_url
db_connection = _backend.db_connection
migrated_db = _backend.migrated_db
session = _backend.session
