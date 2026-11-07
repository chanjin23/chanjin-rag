#!/usr/bin/env bash
# 루프 진입점: 전용 git worktree(loop/mvp 브랜치)를 준비하고 그 안에서 루프 실행기를 돌린다.
# 실행: 저장소 루트에서 `bash scripts/loop.sh [옵션]` (Git Bash)
#   --dry-run          실행 계획과 첫 프롬프트만 출력
#   --status           진행 상태 보고서 출력
#   --only T05         이 작업만
#   --from T05         이 작업부터
#   --retry-blocked    막힌 작업을 다시 시도
#   --include-deferred 미뤄 둔 작업(T18: HWP·HWPX·XLSX)도 실행
# 자세한 내용: loop/README.md
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONFIG="$ROOT/loop/config.json"
read_cfg() { python -c "import json,sys; print(json.load(open(sys.argv[1], encoding='utf-8'))[sys.argv[2]])" "$CONFIG" "$1"; }
BRANCH="$(read_cfg branch)"
WORKTREE="$(cd "$ROOT" && realpath -m "$(read_cfg worktree)")"

# 이미 루프 worktree 안에서 실행했으면 그대로 진행한다.
if [[ "$(git rev-parse --abbrev-ref HEAD)" == "$BRANCH" ]]; then
  exec python "$ROOT/scripts/loop/loop.py" "$@"
fi

# 루프 설정 파일은 worktree에서도 보여야 하므로 커밋돼 있어야 한다.
if [[ -n "$(git status --porcelain -- loop scripts/loop.sh scripts/loop)" ]]; then
  echo "loop/ 또는 scripts/loop*에 커밋되지 않은 변경이 있습니다. 먼저 커밋하세요." >&2
  exit 2
fi

if [[ ! -d "$WORKTREE" ]]; then
  if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git worktree add "$WORKTREE" "$BRANCH"
  else
    git worktree add -b "$BRANCH" "$WORKTREE" HEAD
  fi
fi

echo "루프 worktree: $WORKTREE (브랜치 $BRANCH)"
cd "$WORKTREE"
exec python "$WORKTREE/scripts/loop/loop.py" "$@"
