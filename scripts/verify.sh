#!/usr/bin/env bash
# 작업 완료 전 전체 검사: 백엔드 단위 테스트 → 린트·타입 → 웹 빌드 → E2E(실제 서버 + Chrome).
# 실행: 저장소 루트에서 `bash scripts/verify.sh` (Git Bash). 하나라도 실패하면 즉시 멈추고 0이 아닌 코드로 끝난다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf '\n==> %s\n' "$1"; }

# 루프 실행기(scripts/loop/loop.py)가 LOOP_REPORT_DIR을 주면 테스트 결과를 파일로도 남긴다.
# (pytest → pytest.xml, Playwright → e2e.json) 필수 테스트가 실제로 통과했는지 이름으로 확인하는 데 쓴다.
PYTEST_REPORT=()
if [[ -n "${LOOP_REPORT_DIR:-}" ]]; then
  mkdir -p "$LOOP_REPORT_DIR"
  PYTEST_REPORT=(--junitxml "$LOOP_REPORT_DIR/pytest.xml")
fi

step "1/5 Python 의존성 동기화"
uv sync --all-packages --quiet

step "2/5 백엔드 단위 테스트 (pytest)"
uv run pytest -q "${PYTEST_REPORT[@]}"

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
