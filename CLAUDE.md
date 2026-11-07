# chanjin-rag 작업 지침

개인 문서 RAG 플랫폼. 요구사항은 `docs/PRD.md`, 설계는 `docs/TRD.md`를 따른다. 둘이 어긋나면 PRD가 우선이고 TRD를 고친다.

## 작업 완료 규칙 (반드시 지킬 것)

1. 작업을 끝내기 전에 저장소 루트에서 `bash scripts/verify.sh`를 실행한다.
   - 순서: pytest → ruff·mypy → 웹 타입 검사·빌드 → Playwright E2E
   - E2E는 백엔드(:8001)와 웹(:5174)을 직접 띄우고, 설치된 Chrome으로 화면을 조작한 뒤, 백엔드 로그 파일까지 확인한다.
2. 모두 통과해야 완료로 보고한다. 실패하면 원인을 고치고 다시 실행한다. 고치지 못했으면 실패 내용을 그대로 보고한다.
3. 완료 보고에는 각 단계의 결과(예: `pytest 3 passed`, `E2E 4 passed`)를 적는다.
4. 사용자가 보는 기능을 추가하거나 바꾸면 `web/e2e/`의 E2E 시나리오도 같이 추가·수정한다. 시나리오는 아래 세 가지를 모두 확인한다.
   - 화면 조작: 입력, 클릭
   - 화면 결과: 보여야 할 텍스트와 상태
   - 백엔드 처리: `waitForBackendLog`로 해당 요청의 로그 이벤트 확인
5. 새 API나 백그라운드 처리를 만들면 E2E와 운영에서 추적할 수 있도록 구조화 로그 이벤트를 남긴다(예: `chat.message.received`).
6. 확인하려고 백그라운드로 띄운 개발 서버는 작업이 끝나면 정리한다.

## 실행

| 목적 | 명령 | 포트 |
| --- | --- | --- |
| 백엔드 개발 서버 | `cd backend && uv run uvicorn app.main:app --reload` | 8000 |
| 웹 개발 서버 | `cd web && corepack pnpm dev` | 5173 |
| 전체 검사 | `bash scripts/verify.sh` | |
| E2E만 | `cd web && corepack pnpm e2e` | 8001, 5174 (E2E 전용) |
| E2E 화면 보며 실행 | `cd web && corepack pnpm e2e:headed` | |
| 실패 리포트 | `cd web && corepack pnpm e2e:report` | |

- E2E 포트는 개발 서버와 다르므로, 개발 서버를 띄운 채로 E2E를 돌려도 된다.
- 백엔드 로그를 파일로도 남기려면 `LOG_FILE=경로`, JSON 형식은 `LOG_FORMAT=json`을 설정한다. E2E는 둘 다 자동으로 설정한다.

## 코드 규칙

- Python 3.12, uv workspace(`backend`, `agent`, `eval`). 줄 길이 100, mypy strict.
- 웹: React + TypeScript + Tailwind CSS. pnpm은 `corepack pnpm`으로 실행한다.
- SSE 이벤트 이름과 순서는 TRD 7.3을 따른다(status → sources → token → done / error). 백엔드 `app/api/chat.py`와 웹 `src/api/chat.ts`를 함께 고친다.
- 비밀값(`NVIDIA_API_KEY` 등)은 `.env`에만 두고 커밋하지 않는다.
- 커밋과 푸시는 요청받았을 때만 한다.
