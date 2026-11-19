# api·worker·migrate 공용 이미지 (TRD 3.2). 빌드 컨텍스트는 저장소 루트.
#   docker compose -f deploy/compose.yaml build
# 베이스 이미지는 모두 멀티 아키텍처(amd64·arm64)라 OCI VM(aarch64)에서 그대로 빌드된다(TRD 12.3).
FROM ghcr.io/astral-sh/uv:0.9 AS uv

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    TZ=Asia/Seoul

# PostgreSQL 17 클라이언트(pg_dump, PGDG 저장소)와 age: 백업에 쓴다(TRD 12.2)
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg tzdata age; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL --retry 5 --retry-all-errors https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-17; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/*; \
    pg_dump --version; age --version

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# 의존성 먼저(코드가 바뀌어도 이 층은 캐시). uv workspace라 다른 멤버의 pyproject도 있어야 한다.
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/pyproject.toml
COPY agent/pyproject.toml agent/pyproject.toml
COPY eval/pyproject.toml eval/pyproject.toml
RUN uv sync --frozen --no-dev --package chanjin-rag-backend --no-install-workspace

# 코드. 편집 가능 설치라 코드가 /app/backend에 그대로 있어 config.REPO_ROOT가 /app이 된다
# (alembic.ini, deploy/kiwi_user_dict.txt 경로가 개발 환경과 같다).
COPY backend/ backend/
COPY deploy/kiwi_user_dict.txt deploy/kiwi_user_dict.txt
RUN uv sync --frozen --no-dev --package chanjin-rag-backend

# 볼륨 마운트 지점(tmp, backups). 이름 있는 볼륨은 처음 만들 때 이 소유자를 물려받는다.
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /data/tmp /data/backups \
    && chown -R app:app /data
USER app

WORKDIR /app/backend
EXPOSE 8000
# 기본은 API. worker·migrate는 compose의 command로 바꾼다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
