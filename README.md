# chanjin-rag

개인 문서 RAG 플랫폼. 요구사항은 [docs/PRD.md](docs/PRD.md), 설계는 [docs/TRD.md](docs/TRD.md).

## 구조

| 경로 | 내용 |
| --- | --- |
| `backend/` | FastAPI API 서버와 Ingestion Worker (`app/`), Alembic, 테스트 |
| `agent/` | Windows Sync Agent (`rag-agent` CLI) |
| `eval/` | 평가 CLI (`rag-eval`)와 평가셋 |
| `web/` | Vite + React + TypeScript + Tailwind CSS |
| `deploy/` | Docker Compose, Nginx, PostgreSQL 설정, 백업·복구 스크립트 |

## 개발 실행

필요한 도구: uv, Node.js(corepack으로 pnpm 사용)

```bash
# 1) Python 의존성 (Python 3.12는 uv가 자동으로 받음)
uv sync --all-packages

# 2) DB 스키마 올리기 (DATABASE_URL의 DB에 alembic upgrade head)
uv run rag-backend migrate

# 3) 백엔드 (http://127.0.0.1:8000)
cd backend
uv run uvicorn app.main:app --reload

# 4) 웹 (http://127.0.0.1:5173, /api는 백엔드로 프록시)
cd web
corepack pnpm install
corepack pnpm dev
```

채팅 화면에서 질문을 보내면 백엔드 로그에 `chat.message.received`가 찍히고, 스텁 답변이 SSE로 스트리밍됩니다.

## 검사

```bash
bash scripts/verify.sh   # pytest → ruff·mypy → 웹 빌드 → E2E 전체 (Git Bash)
```

- Docker가 필요합니다. `verify.sh`가 테스트 DB(ParadeDB, `127.0.0.1:5433`, 데이터는 tmpfs)를 `deploy/compose.test.yaml`로 띄우고, 다음 실행을 위해 내리지 않습니다. 내리려면 `docker compose -f deploy/compose.test.yaml down`.
- pytest와 E2E는 `APP_ENV=test`, `PROVIDER=fake`로 실행되어 `.env`의 실제 키를 읽지 않고, 모델 호출은 결정적인 Fake Provider가 대신합니다.
- `SAMPLE_DOCS_DIRS`(실제 문서 폴더, `;`로 구분)를 주면 `pytest -m samples` 단계가 추가로 돕니다.
- pytest는 시작할 때 테스트 DB를 `alembic downgrade base` → `upgrade head`로 비우고 다시 만듭니다. E2E 백엔드도 뜨기 전에 `rag-backend migrate`를 실행합니다.

E2E만 돌릴 때는 `cd web && corepack pnpm e2e`를 실행합니다. 백엔드(:8001)와 웹(:5174)을 직접 띄우고, 설치된 Chrome으로 질문을 입력합니다. 그런 다음 화면 응답과 백엔드 로그(`web/e2e/.logs/backend.log`)를 확인합니다. 화면을 보면서 돌리려면 `pnpm e2e:headed`를, 실패 리포트는 `pnpm e2e:report`를 씁니다.
