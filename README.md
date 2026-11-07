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

# 2) 백엔드 (http://127.0.0.1:8000)
cd backend
uv run uvicorn app.main:app --reload

# 3) 웹 (http://127.0.0.1:5173, /api는 백엔드로 프록시)
cd web
corepack pnpm install
corepack pnpm dev
```

채팅 화면에서 질문을 보내면 백엔드 로그에 `chat.message.received`가 찍히고, 스텁 답변이 SSE로 스트리밍됩니다.

## 검사

```bash
bash scripts/verify.sh   # pytest → ruff·mypy → 웹 빌드 → E2E 전체 (Git Bash)
```

E2E만 돌릴 때는 `cd web && corepack pnpm e2e`를 실행합니다. 백엔드(:8001)와 웹(:5174)을 직접 띄우고, 설치된 Chrome으로 질문을 입력합니다. 그런 다음 화면 응답과 백엔드 로그(`web/e2e/.logs/backend.log`)를 확인합니다. 화면을 보면서 돌리려면 `pnpm e2e:headed`를, 실패 리포트는 `pnpm e2e:report`를 씁니다.
