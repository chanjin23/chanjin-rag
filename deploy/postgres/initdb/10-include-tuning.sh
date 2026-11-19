#!/bin/sh
# DB를 처음 초기화할 때 한 번만 실행된다(docker-entrypoint-initdb.d).
# postgresql.conf 끝에 조정값 파일(deploy/postgres/tuning.conf) include를 붙인다.
# 이미 있는 pgdata에는 실행되지 않으므로, 그때는 같은 줄을 직접 붙이고 db를 재시작한다.
set -eu
conf="$PGDATA/postgresql.conf"
line="include_if_exists = '/etc/postgresql/tuning.conf'"
grep -qxF "$line" "$conf" || printf '\n# chanjin-rag 조정값 (TRD 12.5)\n%s\n' "$line" >> "$conf"
