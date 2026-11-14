#!/usr/bin/env bash
# 백업에서 DB를 복구한다 (TRD 12.2 복구 절차). 새 VM(또는 같은 VM)에서 실행한다.
#
#   1. Compose로 db만 띄운다.
#   2. (PC에서) 백업 파일을 VM으로 복사해 둔다: tailscale file cp / scp rag-YYYYMMDD.dump.age vm:~/
#   3. age -d -i <개인키>로 복호화한다. 개인키를 VM에 두고 싶지 않으면 PC에서 복호화한 평문 덤프
#      (`*.dump`)를 옮겨 와 그 파일을 넘긴다.
#   4. pg_restore로 복원한 뒤 api와 worker를 띄운다.
#   5. 다음 Agent 실행에서 manifest 비교로 백업 이후 바뀐 파일만 다시 올라온다.
#
# 사용:
#   deploy/scripts/restore.sh rag-20261115.dump.age ~/.age/rag-backup.key
#   deploy/scripts/restore.sh rag-20261115.dump          # PC에서 이미 복호화한 평문 덤프
# 환경 변수: COMPOSE_FILE(기본 deploy/compose.yaml), DB_SERVICE(기본 db), POSTGRES_USER(기본 rag),
#           POSTGRES_DB(기본 rag), YES=1(확인 질문 생략)
set -euo pipefail

backup="${1:?백업 파일 경로가 필요합니다(rag-YYYYMMDD.dump.age 또는 평문 .dump)}"
identity="${2:-}"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/compose.yaml}"
DB_SERVICE="${DB_SERVICE:-db}"
PGUSER="${POSTGRES_USER:-rag}"
PGDB="${POSTGRES_DB:-rag}"
compose=(docker compose -f "$COMPOSE_FILE")

[[ -f "$backup" ]] || { echo "파일이 없습니다: $backup" >&2; exit 1; }

# 옆에 .sha256이 있으면 먼저 검증한다
if [[ -f "$backup.sha256" ]]; then
  ( cd "$(dirname "$backup")" && sha256sum -c "$(basename "$backup").sha256" )
fi

if [[ "${YES:-}" != "1" ]]; then
  read -r -p "DB '$PGDB'의 현재 내용을 백업으로 덮어씁니다. 계속할까요? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "취소했습니다"; exit 1; }
fi

echo "1) db만 띄웁니다"
"${compose[@]}" up -d --wait "$DB_SERVICE"
"${compose[@]}" stop api worker >/dev/null 2>&1 || true

workdir="$(mktemp -d)"
chmod 700 "$workdir"
trap 'rm -rf "$workdir"' EXIT
plain="$workdir/restore.dump"

if [[ "$backup" == *.age ]]; then
  [[ -n "$identity" ]] || { echo "암호화된 백업에는 age 개인키 경로가 필요합니다" >&2; exit 1; }
  command -v age >/dev/null || { echo "age가 없습니다" >&2; exit 1; }
  echo "3) age로 복호화합니다"
  age -d -i "$identity" -o "$plain" "$backup"
else
  cp "$backup" "$plain"
fi

echo "4) pg_restore로 복원합니다"
"${compose[@]}" exec -T "$DB_SERVICE" pg_restore --clean --if-exists --no-owner \
  -U "$PGUSER" -d "$PGDB" < "$plain"

echo "   api와 worker를 띄웁니다"
"${compose[@]}" up -d api worker

echo "복구 완료. 다음 'rag-agent sync'에서 백업 이후 바뀐 파일만 다시 올라옵니다."
