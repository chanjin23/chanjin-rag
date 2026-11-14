#!/usr/bin/env bash
# VM 배포 (TRD 12.3): git pull → docker compose build → docker compose up -d.
# arm64 이미지를 VM에서 직접 빌드하므로 레지스트리와 크로스 빌드가 필요 없다.
# migrate 서비스가 alembic upgrade head를 마친 뒤에 api·worker가 뜬다.
# 되돌리기 어려운 마이그레이션 전에는 먼저 `deploy/scripts/backup.sh --compose`로 백업을 받는다.
#
# 사용: 저장소 루트 또는 어디서든 `bash deploy/scripts/deploy.sh [--no-pull]`
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo ".env가 없습니다. .env.example을 복사해 채운 뒤 다시 실행하세요(권한 600)." >&2
  exit 1
fi

COMPOSE=(docker compose -f deploy/compose.yaml --env-file .env)

if [[ "${1:-}" != "--no-pull" ]]; then
  echo "==> git pull"
  git pull --ff-only
fi

echo "==> docker compose build"
"${COMPOSE[@]}" build

echo "==> docker compose up -d"
"${COMPOSE[@]}" up -d --remove-orphans

"${COMPOSE[@]}" ps
echo "배포 완료: http://127.0.0.1:8080 (Tailnet에서는 tailscale serve 주소)"
