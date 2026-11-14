#!/usr/bin/env bash
# DB 암호화 백업을 지금 한 번 만든다 (TRD 12.2). 평소에는 Worker가 매일 03:00(한국 시간)에 같은 일을 한다.
#
#   pg_dump -Fc → age -r <BACKUP_AGE_RECIPIENT> → $BACKUP_DIR/rag-YYYYMMDD.dump.age (+ .sha256)
#   VM에는 최근 BACKUP_KEEP_ON_VM(기본 3)개만 남긴다. 평문 덤프는 디스크에 남기지 않는다.
#
# 사용:
#   deploy/scripts/backup.sh            # 이 셸에서 직접(pg_dump·age 필요)
#   COMPOSE_FILE=deploy/compose.yaml deploy/scripts/backup.sh --compose
#                                        # worker 컨테이너 안에서 `rag-backend backup` 실행(권장)
# 환경 변수: DATABASE_URL(postgresql://… 또는 postgresql+psycopg://…), BACKUP_AGE_RECIPIENT(age 공개키),
#           BACKUP_DIR(기본 ./backups), BACKUP_KEEP_ON_VM(기본 3), TZ(기본 Asia/Seoul)
set -euo pipefail

if [[ "${1:-}" == "--compose" ]]; then
  exec docker compose ${COMPOSE_FILE:+-f "$COMPOSE_FILE"} exec -T worker rag-backend backup
fi

: "${DATABASE_URL:?DATABASE_URL이 필요합니다}"
: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT(age 공개키)가 필요합니다}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP="${BACKUP_KEEP_ON_VM:-3}"
export TZ="${TZ:-Asia/Seoul}"

command -v pg_dump >/dev/null || { echo "pg_dump가 없습니다(PostgreSQL 17 클라이언트)" >&2; exit 1; }
command -v age >/dev/null || { echo "age가 없습니다" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
name="rag-$(date +%Y%m%d).dump.age"
part="$BACKUP_DIR/.${name}.part"
trap 'rm -f "$part"' EXIT

# SQLAlchemy 드라이버 표기(+psycopg)는 libpq가 모르므로 뺀다
url="${DATABASE_URL/+psycopg/}"
# 평문 덤프를 파일로 쓰지 않고 바로 암호화한다
pg_dump -Fc --dbname="$url" | age -r "$BACKUP_AGE_RECIPIENT" -o "$part"
mv -f "$part" "$BACKUP_DIR/$name"
( cd "$BACKUP_DIR" && sha256sum "$name" > "$name.sha256" )

# 날짜순으로 최근 $KEEP개만 남긴다(옆의 .sha256도 함께)
mapfile -t all < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'rag-[0-9]*.dump.age' -printf '%f\n' | sort)
excess=$(( ${#all[@]} - KEEP ))
for (( i = 0; i < excess; i++ )); do
  rm -f "$BACKUP_DIR/${all[$i]}" "$BACKUP_DIR/${all[$i]}.sha256"
done

size=$(stat -c %s "$BACKUP_DIR/$name")
echo "백업 완료: $BACKUP_DIR/$name ($size bytes), 남은 백업 $(( ${#all[@]} - (excess > 0 ? excess : 0) ))개"
