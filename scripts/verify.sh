#!/usr/bin/env bash
# 작업 완료 전 전체 검사: 테스트 DB → 백엔드 단위 테스트 → 린트·타입 → 웹 빌드 → E2E(실제 서버 + Chrome).
# 실행: 저장소 루트에서 `bash scripts/verify.sh` (Git Bash). 하나라도 실패하면 즉시 멈추고 0이 아닌 코드로 끝난다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf '\n==> %s\n' "$1"; }

# 루프 실행기(scripts/loop/loop.py)가 LOOP_REPORT_DIR을 주면 테스트 결과를 파일로도 남긴다.
# (pytest → pytest.xml, 샘플 문서 pytest → pytest-samples.xml, Playwright → e2e.json)
# 필수 테스트가 실제로 통과했는지 이름으로 확인하는 데 쓴다.
PYTEST_REPORT=()
PYTEST_SAMPLES_REPORT=()
if [[ -n "${LOOP_REPORT_DIR:-}" ]]; then
  mkdir -p "$LOOP_REPORT_DIR"
  PYTEST_REPORT=(--junitxml "$LOOP_REPORT_DIR/pytest.xml")
  PYTEST_SAMPLES_REPORT=(--junitxml "$LOOP_REPORT_DIR/pytest-samples.xml")
fi

# 테스트 DB(ParadeDB, 5433). TEST_DATABASE_URL을 직접 주면 그 DB를 그대로 쓰고 컨테이너를 띄우지 않는다.
TEST_DB_COMPOSE=(docker compose -f "$ROOT/deploy/compose.test.yaml")
TEST_DB_PRESET="${TEST_DATABASE_URL:-}"
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql+psycopg://rag:rag@127.0.0.1:5433/rag_test}"

step "1/5 Python 의존성 동기화"
uv sync --all-packages --quiet

step "테스트 DB 준비 (ParadeDB :5433)"
if [[ -n "$TEST_DB_PRESET" ]]; then
  echo "TEST_DATABASE_URL이 주어져 그 DB를 사용합니다"
else
  # 이미 떠 있으면 그대로 재사용한다(up -d는 바뀐 것이 없으면 컨테이너를 다시 만들지 않음).
  # 다음 실행에서 재사용하도록 끝나도 내리지 않는다. 내리기: docker compose -f deploy/compose.test.yaml down
  "${TEST_DB_COMPOSE[@]}" up -d --wait
fi

step "2/5 백엔드 단위 테스트 (pytest)"
uv run pytest -q "${PYTEST_REPORT[@]}"

# 실제 문서 폴더(개인·고객사 문서)를 읽는 테스트. SAMPLE_DOCS_DIRS가 있을 때만 실행한다.
if [[ -n "${SAMPLE_DOCS_DIRS:-}" ]]; then
  step "샘플 문서 테스트 (pytest -m samples)"
  uv run pytest -q -m samples "${PYTEST_SAMPLES_REPORT[@]}"
fi

step "3/5 Python 린트·포맷·타입 (ruff, mypy)"
uv run ruff check .
uv run ruff format --check .
uv run mypy backend/app agent/rag_agent eval/rag_eval

cd "$ROOT/web"
step "4/5 웹 타입 검사와 빌드"
corepack pnpm install --frozen-lockfile --silent
corepack pnpm build

step "5/5 E2E (백엔드 :8001 + 웹 :5174 + Chrome)"
corepack pnpm e2e

printf '\n모든 검사 통과\n'
