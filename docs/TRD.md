# 개인 문서 RAG 플랫폼 TRD

Nov 7, 2026 · @이찬진 · 기준 문서: [PRD.md](./PRD.md)

## 1. 개요

PRD에서 정한 기능을 어떤 기술로, 어떤 구조로 만들지 정한 기술 요구사항 문서입니다. 무엇을 만들지는 PRD를, 어떻게 만들지는 이 문서를 따릅니다. 두 문서가 어긋나면 PRD의 요구사항을 우선하고 이 문서를 고칩니다.

**설계 원칙**

- **컨테이너를 적게:** VM이 2 CPU·12GB이고 사용자가 1명이라 Redis, Celery, 별도 검색엔진을 두지 않습니다.
- **상태는 PostgreSQL 하나에:** 문서, 청크, 작업 대기열, 세션, 로그를 모두 DB에 두어 백업 하나로 복구가 끝나게 합니다.
- **arm64 우선:** 모든 이미지는 VM에서 직접 빌드하거나 arm64 공식 이미지를 씁니다.
- **모델 교체 가능:** LLM, 임베딩, 재정렬 호출은 Provider 인터페이스 뒤에 두고, 모델명과 엔드포인트는 설정값으로만 바꿉니다.

**PRD 대비 구체화한 내용**

| 항목 | 내용 |
| --- | --- |
| 웹의 "지금 동기화"(F-03) | VM은 PC를 깨울 수 없으므로, Sync Agent를 `chanjin-sync://` 사용자 지정 URL 프로토콜로 등록해 브라우저가 PC의 Agent를 직접 실행합니다 |
| 동기화 설정 위치 | 루트 폴더와 제외 패턴은 서버 DB에 저장하고, Agent가 실행할 때마다 받아 갑니다. 웹에서 바꾸면 다음 실행부터 반영됩니다. 루트는 `C:\Chanjin\dev` 하위만 허용하고, 고정 제외(`/project/`, `/backup/`)는 설정과 별개로 코드에 둡니다 |
| 변경 판정 | Agent가 전체 목록(manifest)을 보내고 서버가 업로드 대상을 정합니다. DB를 백업에서 복구해도 차이만 다시 올라옵니다 |
| 파일 이동 | 사라진 경로와 같은 해시의 새 경로가 같은 manifest에 있으면 문서의 경로만 바꿉니다. 옛 경로가 남아 있으면(복사) 청크를 복사합니다. 둘 다 하루 임베딩 한도를 쓰지 않습니다 |
| 문서 삭제 | 범위에서 빠진 문서는 설정 저장 시 즉시, 로컬 삭제 문서는 30일 뒤 영구 삭제합니다(6.5) |
| 원본 파일 위치 보기 | URL 프로토콜에 `reveal` 동작을 추가해 탐색기에서 파일을 선택한 상태로 엽니다. URL에는 경로 대신 해시만 넣습니다(10.2) |
| 임베딩 한도 차감 | 파일 버전(`document_id`, `sha256`)당 한 번만 차감하고, 내용이 같은 청크는 기존 임베딩을 재사용합니다. 하루 청크 상한을 안전장치로 둡니다(6.2) |
| 임베딩 차원 | 4,000차원을 넘으면 halfvec도 HNSW 색인이 안 되므로, 차원 축소 또는 바이너리 양자화로 대응합니다(6.3) |

## 2. 기술 스택

| 영역 | 기술 | 용도·비고 |
| --- | --- | --- |
| 언어 | Python 3.12, TypeScript | 백엔드·Agent·평가는 Python, 웹은 TypeScript |
| 패키지 | uv(workspace, lock 파일), pnpm | |
| API 서버 | FastAPI, Uvicorn, Pydantic v2, pydantic-settings, python-multipart(Sync 업로드) | 설정은 환경 변수와 저장소 루트의 `.env` 하나에서 읽습니다(실행 위치와 무관) |
| SSE | sse-starlette | 답변 스트리밍 |
| DB | PostgreSQL 17(ParadeDB 공식 이미지), pgvector, pg\_search | 벡터, BM25, 필터를 SQL 하나로 처리 |
| DB 접근 | SQLAlchemy 2.0(async), psycopg 3, Alembic | Hybrid 검색 SQL은 직접 작성 |
| 작업 큐 | PostgreSQL `jobs` 테이블 + `SELECT … FOR UPDATE SKIP LOCKED` | 별도 브로커 없음 |
| 예약 작업 | APScheduler(Worker 안) | 자정 한도 초기화, 매일 백업, 로컬 삭제 문서 영구 삭제, 끊긴 동기화 실행 정리, 오래된 임시 파일 정리 |
| 질의 흐름 | LangGraph(1.x) | LangChain 체인과 래퍼는 쓰지 않고 Provider를 직접 호출. 의존하는 `uuid-utils`는 0.19의 네이티브 DLL을 개발 PC의 Windows 애플리케이션 제어 정책이 막아 `<0.19`로 제한(루트 `pyproject.toml`의 `constraint-dependencies`) |
| 모델 호출 | openai SDK 2.x(`base_url`=NVIDIA), httpx, tenacity | 재정렬은 OpenAI 호환이 아니어서 httpx로 호출. openai 3.x는 httpx 대신 httpx2를 써서 2.x로 고정 |
| 한국어 형태소 | kiwipiepy | BM25용 사전 토큰화 |
| 토큰 수 계산 | tiktoken(o200k\_base) 근사 | 청크 길이 기준. 실제 한도는 API의 `truncate=END`로 보호 |
| 문서 파서 | PyMuPDF, python-docx, openpyxl, python-pptx, lxml(HWPX), pyhwp(HWP), charset-normalizer | 3.4 참고 |
| 인증 | argon2-cffi | 비밀번호 해시 |
| 로그 | structlog(JSON, stdout) | 질의 로그는 DB에 저장 |
| CLI | Typer | 백엔드 관리, Agent, 평가 |
| 웹 | Vite, React, TypeScript, Tailwind CSS, React Router, TanStack Query, shadcn/ui | |
| 웹 부가 | react-markdown, remark-gfm, openapi-typescript | API 타입은 FastAPI OpenAPI에서 생성 |
| Sync Agent | httpx, keyring, SQLite(표준 라이브러리), pathspec, Typer | Windows 작업 스케줄러가 실행 |
| 백업 암호화 | age | VM에는 공개키만 둡니다 |
| 리버스 프록시 | Nginx | 정적 파일 서빙, `/api` 프록시 |
| 네트워크 | Tailscale(VM 호스트에 설치), `tailscale serve` | `*.ts.net` 인증서로 HTTPS |
| 배포 | Docker Compose | 이미지는 VM에서 직접 빌드 |
| 테스트·품질 | pytest, pytest-asyncio, ruff, mypy, Vitest, MSW, ESLint, Prettier | |

## 3. 시스템 구성

### 3.1 배포 구성

```mermaid
flowchart LR
    subgraph PC["내 PC (Windows 11)"]
        AGENT["Sync Agent<br/>(uv 가상환경)"]
        TS1["작업 스케줄러<br/>하루 4회"]
        PROTO["chanjin-sync://<br/>URL 프로토콜"]
        BROWSER["브라우저"]
        BACKUP[("C:\Chanjin\dev\backup<br/>암호화 덤프 7개")]
        TS1 --> AGENT
        BROWSER -- "지금 동기화" --> PROTO --> AGENT
        AGENT --> BACKUP
    end

    subgraph VM["OCI VM · Oracle Linux 9 (aarch64)"]
        TSS["tailscale serve<br/>HTTPS :443"]
        subgraph COMPOSE["Docker Compose"]
            WEB["web<br/>Nginx + React 빌드"]
            API["api<br/>FastAPI · Uvicorn"]
            WORKER["worker<br/>작업 큐 · APScheduler"]
            DB[("db<br/>ParadeDB (PG17)")]
        end
        VOL[("볼륨<br/>pgdata · tmp · backups")]
        TSS --> WEB
        WEB -- "/api" --> API
        API --> DB
        WORKER --> DB
        DB --- VOL
        WORKER --- VOL
        API --- VOL
    end

    NV["NVIDIA API Catalog"]

    AGENT -- "HTTPS (Tailnet)" --> TSS
    BROWSER -- "HTTPS (Tailnet)" --> TSS
    API --> NV
    WORKER --> NV
```

### 3.2 컨테이너

| 서비스 | 이미지 | 역할 | 메모리 상한(초기값) |
| --- | --- | --- | --- |
| db | ParadeDB 공식 이미지(arm64) | PostgreSQL 17 + pg\_search + pgvector | 4GB |
| api | `backend` 이미지 | REST API, SSE 답변, 업로드 수신 | 1.5GB |
| worker | `backend` 이미지(실행 명령만 다름) | 추출, 청킹, BM25 색인, 임베딩, 백업 | 3GB |
| web | Nginx(arm64) + React 빌드 결과 | 정적 파일, `/api` 프록시 | 128MB |
| migrate | `backend` 이미지(1회 실행) | `alembic upgrade head` 후 종료 | |

- `api`와 `worker`는 `migrate`가 성공한 뒤에 시작합니다(`depends_on: condition: service_completed_successfully`).
- 남는 약 3GB는 OS, Docker, Tailscale과 HNSW 색인 생성 시 여유분으로 둡니다.
- 볼륨:
  - `pgdata`: DB 데이터
  - `tmp`: 업로드 원본 임시 보관, 추출 후 삭제
  - `backups`: 암호화 덤프, VM에는 최근 3개만 유지

### 3.3 네트워크

- VM의 OCI 보안 목록은 인바운드를 모두 막습니다. Tailscale은 아웃바운드와 NAT 통과만으로 연결됩니다.
- Compose는 공개 포트를 열지 않고, `web`만 `127.0.0.1:8080`에 바인딩합니다. `tailscale serve --https=443 http://127.0.0.1:8080`으로 Tailnet 안에만 노출합니다.
- SSH는 Tailscale SSH 또는 Tailscale IP에만 바인딩한 sshd로 접속합니다.
- Tailscale ACL은 내 기기(PC, 휴대폰 등)만 VM의 443 포트에 접근하도록 제한합니다.
- 외부로 나가는 연결은 NVIDIA API(`integrate.api.nvidia.com` 등), Tailscale, OS·이미지 업데이트뿐입니다.

### 3.4 문서 파서

| 형식 | 라이브러리 | 위치 정보 | 비고 |
| --- | --- | --- | --- |
| PDF | PyMuPDF(1.x) | 페이지 | 텍스트 레이어가 없는 페이지는 "스캔본 의심"(`scanned_page` 경고)으로 기록하고 건너뜁니다(OCR은 후속). AGPL이지만 배포하지 않는 개인 용도라 문제없습니다. 2.x는 onnxruntime 등 무거운 의존성을 끌어와 1.x로 고정합니다 |
| DOCX | python-docx | 제목 계층, 문단 번호, 표 번호·행 | 문단 스타일(Heading 1\~n, `제목 n`, 개요 수준, Title=최상위)로 제목 계층을 만들고, 표는 행 단위로 따로 추출(첫 행을 헤더로, `헤더: 값 \| 헤더: 값`) |
| XLSX | openpyxl(read-only, data\_only) | 시트, 행 범위 | 첫 비어 있지 않은 행을 헤더로 보고, 수식은 계산된 값으로 읽습니다. **후속(T18). 그 전에는 `unsupported_format`** |
| PPTX | python-pptx | 슬라이드 번호 | 제목, 본문 도형, 표, 발표자 노트 순서로 추출 |
| HWPX | zipfile + lxml | 구역, 문단 | `Contents/section*.xml`의 문단과 표를 직접 해석. **후속(T18). 그 전에는 `unsupported_format`** |
| HWP | pyhwp(hwp5txt 계열) | 문단(페이지 없음) | 품질 리스크가 가장 큰 형식입니다. 구축 초기에 내 파일로 비교합니다(13장). **후속(T18). 그 전에는 `unsupported_format`** |
| TXT, MD | charset-normalizer | 줄 범위, MD 제목 | 한국어 Windows 파일은 cp949가 많아 인코딩을 먼저 판별합니다(BOM → UTF-8 → charset-normalizer, EUC-KR 계열은 cp949로 읽음). MD는 ATX 제목(`#`)으로 계층을 만들고 코드 블록 안의 `#`은 무시 |

파서는 `Parser` 인터페이스를 구현하고, 결과는 공통 형식인 `Section(location, heading_path, text, kind)` 목록으로 반환합니다(`backend/app/ingest/parsers/`). `kind`는 `text`, `table`, `slide` 중 하나입니다. 확장자 → 파서 등록표는 대소문자를 무시합니다. 파서마다 타임아웃(60초)과 메모리 상한을 둡니다. 실패하면 사유 코드와 함께 문서를 `failed`로 기록합니다.

**섹션 단위와 위치(`location`)**

| 형식 | 섹션 1개 | `location` 예 |
| --- | --- | --- |
| PDF | 페이지 | `{"page": 3}` |
| DOCX 본문 | 문단(제목 문단은 `heading_path`로만 씀) | `{"paragraph": 12}` |
| DOCX 표 | 데이터 행(`kind=table`, `헤더: 값 \| 헤더: 값`. 행이 1개뿐인 표는 그 행) | `{"table": 2, "row": 5}` |
| PPTX | 슬라이드(`kind=slide`): 제목 → 본문 도형(위→아래, 왼쪽→오른쪽) → 표 → `발표자 노트:` 뒤에 노트 | `{"slide": 7}` |
| TXT, MD | 빈 줄로 나눈 문단 | `{"lines": [10, 14]}` |

섹션을 잘게 나누는 것은 청크 위치를 정확히 남기기 위해서입니다. 청킹(6.3)이 같은 `heading_path`의 연속 섹션을 이어 붙입니다.

**파서 격리(실행 방식)**

- Worker는 `parse_in_subprocess`로 파일마다 하위 프로세스(`python -m app.ingest.parsers._child`)를 띄웁니다. 결과는 자식의 stdout으로 JSON 한 줄을 받습니다(자식은 라이브러리 출력이 섞이지 않게 원래 stdout을 결과 전용으로만 씁니다).
- 타임아웃(`PARSER_TIMEOUT_SECONDS`, 기본 60초)을 넘기면 자식을 죽이고 `timeout`으로 기록합니다.
- 메모리 상한(`PARSER_MEMORY_LIMIT_MB`, 기본 1024MB)은 자식이 시작하자마자 스스로 겁니다. Linux(운영)는 `RLIMIT_AS`, Windows(개발)는 Job Object의 프로세스 메모리 상한(`JOB_OBJECT_LIMIT_PROCESS_MEMORY`, Windows 8 이상은 Job 중첩이 되어 터미널·IDE 안에서도 동작)을 씁니다. 넘으면 할당이 실패해 `memory`로 기록합니다. `RLIMIT_AS`는 가상 메모리 기준이라 실제 사용량보다 빨리 닿을 수 있어 넉넉히 둡니다. 상한을 걸지 못하면 `parse.memory_limit_unavailable` 경고 로그를 남기고 타임아웃만으로 진행합니다.
- 자식이 결과 없이 죽으면 `MemoryError` 흔적이나 SIGKILL(OOM)이면 `memory`, 그 밖에는 `parser_error`입니다. Worker 본체는 영향을 받지 않습니다.
- 로그: `parse.completed`(형식, 섹션 수, 글자 수, 경고 수, 스캔본 의심 페이지 수, 소요 시간), `parse.failed`(사유 코드). 파일 경로·이름과 본문은 남기지 않습니다.

**실패 사유 코드(`failure_code`)**

| 코드 | 의미 | 화면 안내 |
| --- | --- | --- |
| `encrypted` | 암호가 걸린 파일 | 암호를 풀고 저장하면 다음 동기화 때 다시 처리됩니다 |
| `corrupted` | 파일 손상, 형식 불일치 | 파일을 다시 저장해 보세요 |
| `timeout` | 파싱 60초 초과 | 재처리하거나 파일을 나눠 보세요 |
| `memory` | 메모리 상한 초과 | 재처리하거나 파일을 나눠 보세요 |
| `hash_mismatch` | 업로드 중 파일이 바뀜 | 다음 동기화 때 자동으로 다시 올라옵니다 |
| `parser_error` | 그 밖의 파서 예외(`failure_detail`에 메시지, 파일 경로는 지움) | 재처리하세요. 반복되면 파서 문제입니다 |
| `unsupported_format` | 아직 파서가 없는 형식(HWP, HWPX, XLSX. 후속 T18). 파싱을 시도하지 않고, 파일명 청크 1개를 만들어 파일명으로는 검색되게 합니다 | 이 형식은 아직 지원하지 않습니다. 후속 버전에서 지원합니다 |

- XLSX를 Excel 외 도구로 저장해 수식의 계산값이 없으면 빈 값 대신 수식 문자열을 넣고, 문서에 경고를 남깁니다.
- 암호: PDF는 열 때 비밀번호가 필요한 경우(소유자 암호만 있는 PDF는 그대로 읽음), DOCX·PPTX는 OLE 복합 파일 안의 `EncryptedPackage`로 판별합니다. ZIP이 아니거나 내용 형식이 확장자와 다르면 `corrupted`입니다.
- 추출 결과 텍스트가 0자(완전 스캔 PDF, 빈 문서)면 실패가 아니라 `no_text` 상태로 두고, 파일명 청크 1개만 만들어 파일명으로 검색되게 합니다.

## 4. 저장소 구조

uv workspace 하나로 `backend`, `agent`, `eval`을 묶고, 웹은 pnpm으로 따로 관리합니다.

```
chanjin-rag/
├─ backend/                 # api, worker 공용 패키지
│  ├─ app/
│  │  ├─ api/               # FastAPI 라우터 (auth, sync, documents, chat, eval, health)
│  │  ├─ core/              # 설정, 로깅, 보안(세션, 토큰)
│  │  ├─ db/                # SQLAlchemy 모델, 세션, 검색 SQL
│  │  ├─ ingest/            # parsers/, chunking/, tokenize(Kiwi)
│  │  ├─ query/             # LangGraph 그래프, 노드, 프롬프트
│  │  ├─ providers/         # LLM, Embedding, Reranker 인터페이스와 NVIDIA 구현, Fake 구현
│  │  ├─ jobs/              # 작업 큐, Worker 루프, 예약 작업
│  │  ├─ sync/              # Sync API 처리: 실행 기록, manifest 판정, 이동·복사, 업로드 수신
│  │  └─ cli.py             # create-user, reset-password, create-token, reindex, purge 등
│  ├─ alembic/
│  └─ tests/
├─ agent/                   # Windows Sync Agent
│  ├─ rag_agent/            # scan, state, sync(manifest·upload), protocol(run·reveal), install, backup(T14)
│  └─ tests/
├─ eval/                    # 평가 CLI와 평가셋
│  ├─ rag_eval/
│  └─ datasets/             # 평가셋 YAML (Git으로 관리)
├─ web/                     # Vite + React + TypeScript
├─ deploy/
│  ├─ compose.yaml
│  ├─ compose.test.yaml     # 테스트 전용 ParadeDB(5433, tmpfs). verify.sh가 띄움
│  ├─ test-db-init/         # 테스트 DB 초기화 SQL(pg_search, vector 확장)
│  ├─ nginx.conf
│  ├─ postgres/             # postgresql.conf 조정값, 초기화 SQL
│  └─ scripts/              # backup, restore, deploy
├─ docs/                    # PRD.md, TRD.md
├─ .env.example
└─ pyproject.toml           # uv workspace 루트
```

## 5. 데이터 모델

모든 시각은 `timestamptz`로 저장하고, 화면과 일일 한도 계산은 `Asia/Seoul` 기준으로 합니다. 테이블과 핵심 컬럼만 적고, 상세 DDL은 Alembic 마이그레이션이 기준입니다.

- 모델은 `backend/app/db/models.py`, 마이그레이션은 `backend/alembic/versions/`입니다. `rag-backend migrate`(= `alembic upgrade head`)로 올리고 `rag-backend downgrade <리비전>`으로 내립니다.
- 상태·종류 값(`status`, `kind`, `type`, `role`, `reason`, `failure_code`, `failure_type`, `source`)은 PostgreSQL 네이티브 enum 대신 문자열 + CHECK 제약으로 저장합니다. 값을 추가할 때 `ALTER TYPE` 없이 제약만 바꾸면 됩니다.
- `sync_config` 초기값(마이그레이션이 넣음): 루트 `C:\Chanjin\dev`, 기본 제외 패턴(6.1), `max_file_mb` 10.

### 5.1 수집

| 테이블 | 핵심 컬럼 | 설명 |
| --- | --- | --- |
| `sync_config` | `roots jsonb`, `excludes jsonb`, `max_file_mb int`(기본 10), `held_deletions_approved_at`, `held_deletions_run_id`(보류 삭제 적용 승인, 6.1), `updated_at` | 단일 행. Agent가 실행할 때마다 받아 감. 고정 제외는 여기에 넣지 않고 코드에 둠. `max_file_mb`는 웹에서 바꾸지 않음(Nginx 업로드 상한과 함께 바꿔야 하므로) |
| `documents` | `id`, `path`, `path_key`(NFC 정규화 후 소문자, unique), `filename`, `ext`(소문자, 점 포함: `.md`), `size`, `mtime`, `sha256`, `status`, `failure_code`, `failure_detail`, `content_indexed bool`, `locally_deleted bool`, `deleted_at`, `needs_upload bool`, `embed_charged_sha256`, `embed_model`, `embed_dim`, `created_at`, `updated_at` | 파일 1개 = 1행. `path`는 원래 Windows 경로를 그대로 보관. `embed_charged_sha256`은 한도를 이미 차감한 버전 |
| `sections` | `id`, `document_id`, `seq`, `location jsonb`, `heading_path text[]`, `kind`, `text` | 추출 텍스트 원본. 재청킹은 여기서 다시 하므로 재업로드가 필요 없음 |
| `chunks` | `id`, `document_id`, `seq`, `location jsonb`, `heading_path`, `filename`, `content`, `content_sha256`, `bm25_text`, `token_count`, `embedding halfvec(N)`(NULL 허용, N=`EMBED_DIM`), `embed_model` | 검색 단위. `bm25_text`는 Kiwi 토큰을 공백으로 이은 문자열. `content_sha256`은 임베딩 입력(머리말 포함)의 해시로, 임베딩 재사용에 씀. `filename`은 `documents.filename`의 사본으로, BM25 색인이 한 테이블의 컬럼만 색인할 수 있어 둠(파일 이동 시 함께 갱신) |
| `jobs` | `id`, `type`, `document_id`, `sha256`, `status`, `priority`, `attempts`, `run_after`, `locked_at`, `last_error` | `type`: `extract`, `index`, `embed`. `status`: `queued`, `running`, `done`(버전이 바뀌어 결과를 버린 경우 포함), `failed`(재시도 초과). `priority`는 파일 수정 시각(`timestamptz`). `sha256`은 작업 대상 버전으로, 끝날 때 문서의 현재 해시와 다르면 결과를 버림 |
| `embed_quota` | `day date`(PK), `used int`, `chunks_used int` | 한국 시간 기준 날짜별 사용량(파일 수, 청크 수). 새 날짜 행이 생기면 자연스럽게 초기화 |
| `worker_heartbeats` | `name`(PK), `started_at`, `heartbeat_at`, `pid`, `info jsonb` | Worker 하트비트(12.4). Worker가 30초마다 갱신하고 `/health`가 읽음 |
| `provider_status` | `kind`(PK: `llm`·`embed`·`rerank`), `model`, `last_success_at`, `last_failure_at`, `last_error_code`(7.3 코드), `last_status_code`, `failing_since`(같은 코드로 이어진 연속 실패의 첫 시각, 성공하면 NULL), `updated_at` | 모델 호출 마지막 성공·실패(12.4). API와 Worker가 기록하고 `/health`가 읽음 |
| `sync_runs` | `id`, `source`(`schedule`·`web`·`cli`), `started_at`, `finished_at`, `heartbeat_at`, `status`(`running`·`succeeded`·`failed`·`abandoned`), `stats jsonb` | 동기화 실행 이력. 웹의 상태 화면이 사용. `stats`에 업로드 수, 지원하지 않는 형식으로 건너뛴 수, 읽지 못한 폴더, 보류된 삭제 판정을 담음 |

**`location` 예시:** `{"page": 3}`, `{"sheet": "매출", "rows": [2, 41]}`, `{"slide": 7}`, `{"lines": [120, 168]}`

**문서 상태**

```mermaid
stateDiagram-v2
    [*] --> queued: 업로드 수신
    [*] --> meta_only: 10MB 초과(파일명만)
    queued --> extracting
    extracting --> indexed: 추출·청킹·BM25 완료
    extracting --> no_text: 추출 텍스트 0자(파일명만 검색)
    extracting --> failed: 파싱 실패
    indexed --> embedding: 하루 한도 안에서 선택
    embedding --> ready: 임베딩 완료
    embedding --> indexed: 일시 오류(재시도 대기)
    embedding --> failed: 재시도 초과
    failed --> queued: 재처리 또는 파일 수정(재업로드 후)
    ready --> queued: 파일 수정
    indexed --> queued: 파일 수정
    no_text --> queued: 파일 수정
    meta_only --> queued: 파일 수정(10MB 이하가 됨)
    queued --> meta_only: 파일 수정(10MB 초과가 됨)
```

- `locally_deleted`는 상태와 별개인 표시값입니다. 로컬에서 지워져도 30일간 검색은 되고, 결과에 "로컬 삭제됨"을 붙입니다. 이후 영구 삭제됩니다(6.5).
- 어느 상태에서든 파일이 수정되면(새 `sha256`) 진행 중인 작업은 끝날 때 결과를 버리고, 새 버전으로 `queued`부터 다시 시작합니다.
- 화면의 "처리 중"은 `extracting`, `embedding`을, "BM25만"은 `indexed`를, "텍스트 없음"은 `no_text`를 뜻합니다.

### 5.2 인증

| 테이블 | 핵심 컬럼 | 설명 |
| --- | --- | --- |
| `users` | `id`, `username`, `password_hash`, `failed_logins`, `locked_until` | 1행. `rag-backend create-user` CLI로만 생성(비밀번호 변경·잠금 해제는 `rag-backend reset-password`) |
| `sessions` | `id_hash`, `user_id`, `expires_at`, `last_seen_at` | 쿠키 값의 SHA-256만 저장 |
| `api_tokens` | `id`, `name`, `token_hash`, `last_used_at`, `first_used_at`(Agent 첫 접속, 첫 실행 체크리스트), `revoked_at` | Sync Agent용. `rag-backend create-token` CLI로 발급(평문은 발급 때 한 번만 출력) |

### 5.3 대화, 로그, 평가

| 테이블 | 핵심 컬럼 | 설명 |
| --- | --- | --- |
| `conversations` | `id`, `title`, `created_at`, `updated_at` | F-11. `title`은 첫 질문 앞 40자로 정하고 사용자가 바꿀 수 있음. 삭제하면 메시지, 질의 로그, 피드백도 함께 삭제 |
| `messages` | `id`, `conversation_id`, `role`, `content`, `citations jsonb`, `status`, `created_at` | `status`: `streaming`, `complete`, `aborted`, `error`. `citations`: 번호별 `chunk_id`, `document_id`, 경로, 위치, 수정일, 인용 구절. 화면은 저장된 값으로 그리므로 청크가 바뀌어도 인용은 남음 |
| `query_logs` | `id`, `message_id`, `rewritten_query`, `keyword_query`, `filters`, `retrieved jsonb`, `grade`, `retried bool`, `not_found bool`, `coverage jsonb`, `latency_ms jsonb`, `models jsonb` | `retrieved`: 후보별 RRF 점수, 재정렬 점수, 순위. `coverage`: 질의 시점의 검색 범위. `latency_ms`: 단계별 지연. `source`: `chat`(채팅), `eval`(`rag-eval run`, `message_id` 없음) |
| `feedback` | `id`, `message_id`(unique), `rating`(+1·-1), `reason`, `note`, `failure_type`, `reviewed_at` | 답변당 1개이며 다시 누르면 바뀜. `reason`: `wrong`, `not_found_wrong`(실제로는 문서에 있음), `other`, NULL. `failure_type`(NULL 허용, 검토할 때 분류): `parse`, `chunk`, `retrieval`, `rerank`, `hallucination` (F-12) |
| `eval_runs` | `id`, `dataset_version`, `git_sha`, `config jsonb`, `metrics jsonb`, `started_at`, `finished_at` | F-13 |
| `eval_results` | `run_id`, `item_id`, `retrieved jsonb`, `answer`, `scores jsonb` | 문항별 결과 |

평가셋 문항 자체는 DB가 아니라 `eval/datasets/*.yaml`에서 Git으로 관리합니다. 정답 근거는 청크 ID가 아니라 `(경로, 위치)`로 적습니다. 청킹 방식이 바뀌어 청크 ID가 달라져도 라벨을 다시 달 필요가 없습니다. 파일을 옮기거나 지워 라벨의 경로가 `documents`에 없으면, `rag-eval`이 실행 전에 해당 문항을 경고로 보여주고 지표에서 뺍니다.

### 5.4 색인

| 대상 | 색인 | 비고 |
| --- | --- | --- |
| `chunks.embedding` | HNSW(`halfvec_cosine_ops`), `m=16`, `ef_construction=64` | 필터와 같이 쓸 때 결과가 모자라지 않게 검색 트랜잭션에서 `SET LOCAL hnsw.iterative_scan = relaxed_order`, `hnsw.ef_search = 100`(기본 40이 후보 수 50보다 작음)을 켭니다(7.2) |
| `chunks.bm25_text` | pg\_search BM25 색인, 공백(whitespace) 토크나이저 | Kiwi가 이미 토큰화했으므로 DB에서는 공백으로만 자릅니다. 함께 `filename`, `heading_path`도 색인(`simple` 토크나이저: 구두점·밑줄에서 자르고 소문자화). pg\_search 0.26.5 문법: `USING bm25 (id, (bm25_text::pdb.whitespace), (filename::pdb.simple), (heading_path::pdb.simple))`, 첫 컬럼이 키(`key_field`는 0.26부터 불필요) |
| `documents` | `path_key` unique, `sha256`, `(status)`, `(locally_deleted)` | |
| `sections`, `chunks` | `(document_id, seq)` unique, `chunks.content_sha256` | |
| `jobs` | `(status, type, priority DESC)` | 큐 선택용 |

pg\_search의 색인 생성 문법과 토크나이저 설정은 버전마다 바뀌어 왔습니다. 구축 초기 검증(13장)에서 쓰는 이미지 버전 기준으로 확정합니다.

## 6. 수집 파이프라인

### 6.1 Sync Agent 실행 흐름

```mermaid
sequenceDiagram
    participant A as Sync Agent
    participant L as 로컬 SQLite
    participant S as API 서버
    participant W as Worker

    A->>S: GET /api/sync/config
    S-->>A: roots, excludes, max_file_mb
    A->>S: POST /api/sync/runs (source)
    A->>A: 폴더 순회, 제외 패턴 적용, 확장자 필터
    A->>L: (경로, 크기, 수정 시각)이 같으면 저장된 해시 재사용
    A->>A: 바뀐 파일만 SHA-256 계산
    A->>S: POST /api/sync/runs/{id}/manifest (전체 목록)
    S-->>A: upload / meta_only / deleted / restored 목록
    loop upload 목록
        A->>S: PUT /api/sync/files (multipart)
        S->>W: extract 작업 등록
    end
    A->>S: POST /api/sync/files/meta (10MB 초과)
    A->>S: POST /api/sync/runs/{id}/complete
    A->>S: GET /api/sync/backups/latest
    A->>A: 없는 백업만 내려받아 해시 검증, 7개 초과분 삭제
```

**변경 판정은 서버가 합니다.** manifest의 각 항목(`path`, `size`, `mtime`, `sha256`)을 `documents`와 비교합니다.

위에서부터 순서대로 판정합니다.

| 경우 | 서버 판정 |
| --- | --- |
| 새 경로이고, 같은 해시의 기존 문서 경로가 이번 manifest에 없음 | 이동(`moved`). 기존 문서의 `path`만 새 경로로 바꿈. 업로드 없음, 한도 사용 없음 |
| 새 경로이고, 같은 해시의 기존 문서 경로도 manifest에 있음 | 복사. 기존 sections·chunks·임베딩을 새 문서로 복사. 업로드 없음, 한도 사용 없음 |
| 새 경로이거나 해시가 다름 | 10MB 이하면 `upload`, 초과면 `meta_only` |
| 해시가 같음 | 아무것도 안 함(수정 시각만 갱신) |
| `needs_upload=true`(재처리 요청된 실패 파일) | `upload` |
| DB에는 있고 manifest에 없으며, 읽지 못한 폴더(`scan_errors`) 아래가 아님 | `locally_deleted=true`, `deleted_at` 기록 |
| DB에는 있고 manifest에 없으나, 읽지 못한 폴더 아래임 | 판정 보류(그대로 둠) |
| `locally_deleted`인데 manifest에 다시 나타남 | 표시 해제(`restored`) |

- manifest에는 파일 목록과 함께 `scan_errors`(권한 오류 등으로 읽지 못한 폴더 목록)를 보냅니다.
- **대량 삭제 보호:** 이번 실행에서 새로 삭제로 판정된 문서가 전체 문서의 20%(`MASS_DELETE_GUARD_RATIO`)를 넘으면 삭제 판정을 모두 보류하고, `sync_runs.stats`에 기록해 동기화 화면에 경고를 띄웁니다. 사용자가 화면에서 "삭제 판정 적용"을 누르면 다음 실행 때 적용합니다.
  - 구현: 승인하면 `sync_config.held_deletions_approved_at`·`held_deletions_run_id`를 기록합니다(보류된 경로 전체 목록은 저장하지 않으므로 그 실행의 판정을 그대로 되살리지 않음). 다음 manifest 판정은 보호를 건너뛰고 그때 사라진 문서를 로컬 삭제로 표시하며, 승인은 판정 결과와 상관없이 한 번 쓰고 비웁니다(`stats.manifest.held_deletions_approved`, 로그 `sync.deletion.approval_used`).
- 업로드 시 서버가 다시 계산한 해시가 manifest와 다르면(업로드 사이에 파일이 바뀜) 저장하지 않고 `hash_mismatch`로 응답합니다. Agent는 다음 실행에서 자연스럽게 다시 올립니다.

**판정 세부 규칙(구현: `backend/app/sync/manifest.py`)**

- 삭제 판정 대상은 로컬 삭제 표시가 없고 수집 범위 안(허용 폴더 `SYNC_ALLOWED_ROOT` 하위, `sync_config.roots` 하위, 고정 제외·제외 패턴에 걸리지 않음)인 문서입니다. 범위에서 빠진 문서는 설정 저장 때 영구 삭제하므로(6.5) manifest 판정에서는 건드리지 않습니다.
- 대량 삭제 보호의 비율은 `새 삭제 판정 수 ÷ 판정 전 범위 안 활성 문서 수`이고, 이 값이 `MASS_DELETE_GUARD_RATIO`를 **넘을 때**(같으면 적용) 모두 보류합니다. `stats.deletion_held.mass_delete`에 건수·분모·비율·경로 예시(20개)를, `stats.deletion_held.scan_error`에 읽지 못한 폴더 때문에 보류한 건수를 남깁니다.
- 이동·복사의 원본은 같은 해시·**같은 확장자**의 문서 중 로컬 삭제 표시가 없는 것, ID가 작은 것부터 고릅니다. 확장자가 바뀌면(`.txt`→`.md`) 파서가 달라지고 임시 원본 이름(`{document_id}-{sha256}{ext}`)도 달라지므로 업로드로 판정합니다. 로컬 삭제 표시된 문서가 새 경로로 이동하면 표시도 해제합니다(`moved`와 `restored`에 함께 나옴).
- 옛 경로가 `scan_errors` 아래인 문서는 이동 원본으로 고르지 않습니다. 읽지 못했을 뿐 사라진 것이 아니므로, 새 경로는 복사(추출이 끝났으면) 또는 업로드로 판정하고 옛 문서는 그대로 둡니다.
- 이동은 `path`·`path_key`·`filename`·`ext`와 청크의 `filename`, 파일명 청크(`meta_only`·`no_text`·미지원 형식)의 내용·BM25 토큰을 바꿉니다. `content_sha256`은 저장된 임베딩의 실제 입력 해시이므로 그대로 둡니다.
- 복사는 원본이 추출을 끝낸 경우(대기·추출 중이 아님, `needs_upload` 아님)만 합니다. 아니면 업로드로 판정합니다. 원본의 임베딩이 끝났으면(`ready` 등) 벡터까지 복사하므로 API 호출도 한도 사용도 없고, 복사본의 `embed_charged_sha256`은 자기 해시로 둡니다.
- 원본이 아직 임베딩 전·중(`indexed`·`embedding`)이면 복사할 벡터가 (일부) 없습니다. 복사본은 `indexed` + 일반 `embed` 작업으로 등록하고 `embed_charged_sha256`을 비워 둡니다. 남은 청크를 임베딩하려면 API를 실제로 부르므로 다른 파일과 똑같이 하루 파일 한도를 1 쓰고, 한도가 찼으면 다음 날로 미뤄집니다(한도를 건너뛰게 표시하면 하루 한도를 넘겨 API를 부를 수 있음).
- `upload`·`meta_only` 판정은 문서를 만들지 않습니다. 업로드나 메타 등록이 올 때 접수하므로, 업로드가 실패하면 다음 manifest에서 다시 `upload`로 나옵니다.
- 범위 밖·고정 제외·제외 패턴·지원하지 않는 확장자 항목과 같은 경로의 중복 항목은 판정하지 않고 `rejected`(경로, 이유)로 돌려줍니다.
- 하트비트가 `SYNC_RUN_STALE_MINUTES` 넘게 없는 `running` 실행은 Worker의 APScheduler(1분 간격)가 `abandoned`로 바꿉니다. 그 뒤 하트비트가 다시 오면(PC가 절전에서 깨어남) `running`으로 되돌립니다.

**Agent 쪽 규칙**

- 동시에 한 인스턴스만 실행합니다(Windows 이름 있는 뮤텍스). 이미 실행 중이면 바로 종료합니다.
- 실행 중에는 1분마다 `sync_runs.heartbeat_at`을 갱신합니다. 서버는 10분 넘게 갱신이 없는 `running` 실행을 `abandoned`로 바꿉니다(PC 절전, 강제 종료).
- 업로드 실패는 로컬 SQLite 재시도 큐에 남기고, 다음 실행에서 manifest 판정을 다시 받아 자연스럽게 재시도합니다. 판정 후 업로드 전에 사라진 파일은 건너뜁니다.
- 다른 프로그램이 잠근 파일(열려 있는 Excel 등)은 이번 실행에서 건너뛰고 manifest에도 넣지 않습니다. 삭제로 오판하지 않도록 `scan_errors`에 파일 경로로 넣습니다. 잠금은 해시를 계산하려고 열 때 알 수 있으므로, (크기, 수정 시각)이 같아 캐시된 해시를 쓰는 파일은 manifest에 들어가고 업로드가 필요하면 그때 실패해 재시도 큐로 갑니다. 설정된 루트가 없거나 열리지 않아도 `scan_errors`로 보냅니다.
- 고정 제외(코드에 있음, 웹에서 지울 수 없음): `/project/`, `/backup/`. `C:\Chanjin\dev` 기준이며 앞의 `/`가 있어야 하위의 같은 이름 폴더를 빼지 않습니다.
- 기본 제외 패턴(gitignore 문법, 웹에서 편집 가능): `.git/`, `node_modules/`, `.venv/`, `venv/`, `__pycache__/`, `~$*`, `*.tmp`.
- 지원 확장자(`.pdf .txt .md .docx .hwp .hwpx .xlsx .pptx`)만 대소문자 구분 없이 manifest에 넣습니다. 그 밖의 형식(`.doc`, `.xls`, `.ppt` 등)은 건너뛴 수를 형식별로 `stats`에 남겨 화면에 보여줍니다.
- 경로는 NFC로 정규화해 보냅니다.
- 수집 루트 밖의 경로는 심볼릭 링크라도 따라가지 않습니다.

### 6.2 Worker 처리

Worker는 하나의 프로세스에서 작업 루프와 APScheduler를 함께 돌립니다. 작업은 `SELECT … FOR UPDATE SKIP LOCKED`로 하나씩 가져가고, `locked_at`이 10분 넘게 갱신되지 않은 작업은 중단된 것으로 보고 다시 대기열에 넣습니다. 작업 결과를 저장하는 트랜잭션에서 `jobs.sha256`이 문서의 현재 `sha256`과 다르면 결과를 버리고 작업을 끝냅니다(처리 중에 새 버전이 올라온 경우).

| 단계 | 작업 | 한도 | 내용 |
| --- | --- | --- | --- |
| 1 | `extract` | 없음 | 임시 원본을 파싱해 `sections`를 저장하고, 원본 파일을 삭제. 실패해도 원본은 삭제하고 `failed`와 사유를 기록 |
| 2 | `index` | 없음 | 청킹, Kiwi 토큰화, `chunks` 교체(한 트랜잭션). 이 시점부터 BM25로 검색됨. 이어서 `embed` 작업 등록 |
| 3 | `embed` | 하루 100개 파일 | 최근 수정순으로 선택. 청크를 `EMBED_BATCH_SIZE`개씩 묶어 임베딩 API 호출. 무료 엔드포인트는 여러 문장 묶음에 500을 돌려주므로 기본 1개(13장 실측), 묶음 요청이 500이면 반으로 나눠 다시 보냄 |

**하루 임베딩 한도**

- `embed` 작업을 가져갈 때 같은 트랜잭션에서 `embed_quota`의 오늘(한국 시간) `used`를 확인합니다. 문서의 `embed_charged_sha256`이 작업의 `sha256`과 같으면 이미 차감한 버전이므로 세지 않고 시작합니다. 다르면 `used`가 100 미만일 때만 1 올리고 `embed_charged_sha256`을 기록한 뒤 시작합니다.
- 한도는 버전마다 한 번, 작업을 가져갈 때 차감하고, 실패해도 돌려주지 않습니다. 429로 반납했다가 다시 가져가도 같은 버전이면 다시 세지 않습니다. API 호출량을 넘기지 않는 것이 우선입니다.
- **청크 재사용:** 같은 문서의 이전 청크 중 `content_sha256`과 `embed_model`이 같은 것이 있으면 그 벡터를 복사하고 API를 부르지 않습니다. 새 버전의 모든 청크가 재사용되면 한도를 차감하지 않습니다. 이를 위해 `index` 단계는 이전 청크의 `(content_sha256, embedding)`을 남겨 두었다가 교체 트랜잭션 안에서 바로 채웁니다. 모든 청크가 채워지면 `embed` 작업 없이 바로 `ready`가 됩니다. 그래서 일부만 고친 파일은 한도에 막혀도 대부분의 청크가 계속 의미 검색됩니다.
- **하루 청크 상한(안전장치):** 실제로 API로 보낸 청크 수를 `embed_quota.chunks_used`에 더합니다. `EMBED_DAILY_CHUNK_LIMIT`을 넘으면 파일 한도가 남아 있어도 `embed`를 다음 날까지 쉽니다. 진행 중인 파일은 끝까지 처리합니다. 기본값은 13장 3번(API 한도 실측) 뒤에 정하고, 그 전에는 제한 없음(0)입니다.
- 한도에 도달하면 `embed` 작업은 다음 날 0시(한국 시간)까지 쉬고, `extract`와 `index`는 계속 처리합니다.
- 수정 파일의 재임베딩도 1개로 셉니다. 질문 임베딩은 세지 않습니다.
- 한도 값은 `EMBED_DAILY_FILE_LIMIT` 설정으로 바꿀 수 있습니다.

**재시도:** API 오류(429, 5xx, 타임아웃)는 tenacity로 지수 백오프합니다(최대 5회, 최대 대기 60초). openai SDK 자체 재시도는 끕니다. 끝까지 실패하면 Provider 전용 예외로 바꿉니다: 429 → `RateLimited`, 5xx·타임아웃·연결 실패 → `ProviderUnavailable`, 401·403 → `ProviderAuthError`(NVIDIA는 틀린 키에 403 `Authorization failed`를 돌려줌, 재시도하지 않음), 402(크레딧·사용량 소진) → `ProviderQuotaExhausted`(`ProviderAuthError`의 하위, 오류 코드 `provider_auth`). 429가 계속되면 해당 작업을 `run_after`를 30분 뒤로 미뤄 반납합니다. LLM 스트리밍은 응답 스트림을 여는 데까지만 재시도합니다. 호출마다 `provider.call` 로그(종류, 모델, 지연, 상태 코드, 재시도 횟수. 본문·키 제외)를 남깁니다.

**구현 세부(`backend/app/jobs/`, `backend/app/ingest/{intake,scope,local}.py`)**

- 실행: `rag-backend worker`. 준비 루프(`extract`·`index`, 파싱은 한 번에 하나)와 임베딩 루프(`embed`, API 호출 동시 2개)를 함께 돌립니다. APScheduler는 30초마다 하트비트(`worker_heartbeats`), 1분마다 멈춘 작업 재대기, 매일 0시(한국 시간)에 임베딩 루프를 깨웁니다. 한도는 날짜 키(`embed_quota.day`)가 바뀌어 자연히 초기화됩니다. 매일 03:00 DB 백업(`backup`, 12.2), 매일 04:00 보관 기간이 지난 로컬 삭제 문서 영구 삭제(`retention_purge`, 6.5)도 이 스케줄러가 돌립니다.
- 접수(업로드와 `ingest-local` 공용): `path_key`로 문서를 찾아, 해시가 같고 `needs_upload`가 아니면 아무것도 하지 않습니다. 새 버전이면 상태를 `queued`로 되돌리고, 임시 원본을 `UPLOAD_TMP_DIR/{document_id}-{sha256}{확장자}`에 두고 `extract`를 등록합니다. 10MB 초과는 원본 없이 파일명 청크만 만듭니다(`meta_only`).
- 가져갈 때도 작업의 `sha256`이 문서의 현재 해시와 다르면 바로 버립니다(`done`, `job.discarded_stale`). 그래서 지난 버전의 `embed`는 한도를 차감하지 않습니다.
- 한도가 차면 이미 차감한 버전(429로 반납된 작업 등)만 가져가고, 그것도 없으면 다음 날 0시(한국 시간)까지 쉽니다(`quota.exhausted`, 같은 날·같은 이유는 한 번만 기록). 청크 상한은 차감 여부와 상관없이 새 `embed`를 막습니다.
- `extract` 결과: 텍스트 0자면 `no_text`, `unsupported_format`이면 `failed`로 기록하고 둘 다 파일명 청크 1개를 만들어 파일명 검색이 되게 합니다(`index` 없음). 그 밖의 파싱 실패는 `sections`만 지우고 사유를 기록합니다. 파싱 중 예외로 재시도할 때 원본이 이미 지워졌으면 `failed`(`parser_error`) + `needs_upload=true`로 다음 동기화 때 다시 받습니다.
- `embed`는 남은 청크를 32개씩 저장하며, 저장할 때마다 버전을 확인하고 `locked_at`을 갱신합니다. API 요청 크기는 `EMBED_BATCH_SIZE`입니다. API로 보낸 청크 수는 결과를 버리더라도 `chunks_used`에 셉니다.
- 오류: `RateLimited`·`ProviderAuthError`·`EmbeddingDimensionError`는 `JOB_RATE_LIMIT_DELAY_MINUTES`(30분) 뒤로, 그 밖의 오류는 1·2·4…분(최대 30분) 뒤로 반납합니다. 시도 횟수(`attempts`, 가져갈 때 1 증가)가 `JOB_MAX_ATTEMPTS`(5)에 닿으면 `failed`입니다. `embed`를 반납하면 문서는 `indexed`(BM25 검색은 계속됨)로 돌아갑니다.
- 로그: `job.started`, `job.completed`(단계별 개수: 섹션·청크·재사용·임베딩 수), `job.failed`(오류 종류, 최종 여부, 다음 시도 시각), `job.discarded_stale`, `job.requeued_stale`, `quota.exhausted`, `worker.started`/`worker.stopped`. 경로·파일명·본문은 남기지 않고 문서 ID만 남깁니다.
- 개발·E2E 시드: `rag-backend ingest-local <폴더> [--no-process]`가 Agent 없이 로컬 파일을 같은 접수 경로로 큐에 넣고 처리합니다. 범위 검증(`SYNC_ALLOWED_ROOT` 하위, 고정 제외, `sync_config.excludes`, 지원 확장자)도 같습니다. 테스트·E2E는 `SYNC_ALLOWED_ROOT`를 임시 폴더로 바꿉니다(`ingest.local.started`, `ingest.local.completed`).

### 6.3 청킹과 임베딩

**청킹 규칙(PRD 5.1의 구현)**

- 같은 `heading_path` 안의 문단을 이어 붙여 500\~800토큰으로 만들고, 앞 청크 끝 10\~15%를 겹칩니다. 문단 중간에서는 자르지 않되, 한 문단이 800토큰을 넘으면 문장 단위로 자릅니다.
- 청크 앞에 `[파일명 > 제목 > 소제목]` 머리말을 붙여 임베딩합니다. 화면에 보여주는 `content`에는 넣지 않습니다.
- 표: 헤더 행 + 데이터 행 묶음(최대 약 600토큰)을 `헤더: 값 | 헤더: 값` 형태 텍스트로 바꿉니다. 청크마다 헤더를 반복합니다.
- PPTX: 슬라이드 1장 = 청크 1개(800토큰을 넘으면 나눔), 발표자 노트를 포함합니다.
- `meta_only`, `no_text` 문서: 파일명과 경로만 담은 청크 1개를 만들어 BM25로만 검색되게 합니다.

**청킹 구현 세부(`backend/app/ingest/chunking/`)**

- 토큰 수는 tiktoken `o200k_base`로 셉니다(`chunks.token_count`는 화면용 `content`의 토큰 수). 500\~800 범위도 `content` 기준입니다. 인코딩 파일은 처음 쓸 때 내려받아 캐시하므로, 오프라인 이미지에서는 빌드 때 미리 받아 두고 `TIKTOKEN_CACHE_DIR`로 가리킵니다.
- 묶음: 연속한 섹션 중 `kind`와 `heading_path`가 같은 것끼리 묶습니다. 표는 위치에서 `row`를 뺀 값(예: `{"table": 2}`, XLSX면 `{"sheet": …}`)이 같아야 같은 표로 봅니다. 슬라이드는 언제나 1장씩입니다.
- 문단: 섹션 안에서는 빈 줄로 나눈 덩어리가 문단입니다. 문단을 순서대로 800토큰까지 채우고, 넘칠 문단은 다음 청크로 넘깁니다(그래서 다음 문단이 크면 500토큰보다 작은 청크가 생길 수 있고, 묶음의 마지막 청크도 작을 수 있습니다). 800토큰을 넘는 문단은 문장, 문장도 넘으면 단어, 공백 없는 덩어리는 토큰 경계에서 자릅니다.
- 겹침: 같은 묶음의 두 번째 청크부터 앞 청크 끝의 10\~15%(토큰 기준)를 앞에 붙입니다. 시작점은 문장 시작을 우선하고, 없으면 단어 경계입니다. 겹침을 붙이면 800토큰을 넘는 경우에는 겹침을 뺍니다. 표는 행이 서로 독립이라 겹치지 않습니다.
- 표 헤더: 파서가 행마다 `헤더: 값 | 헤더: 값`으로 만들어 두므로 행을 묶기만 해도 청크마다 헤더가 반복됩니다. 약 600토큰을 넘으면 다음 청크로 넘기고, 한 행이 800토큰을 넘을 때만 그 행을 자릅니다.
- 슬라이드가 800토큰을 넘으면 줄 단위로 나누고 본문과 같은 방식으로 겹칩니다. 나뉜 청크도 위치는 `{"slide": n}` 그대로입니다.
- 머리말: 임베딩 입력은 `[파일명 > 제목 > 소제목]` 줄 + 줄바꿈 + `content`이고, 제목이 없으면 `[파일명]`입니다. `content_sha256`은 이 임베딩 입력의 SHA-256이라, 본문이 같아도 제목이 바뀌면 다시 임베딩합니다.
- 위치(`location`): 섹션 하나로 된 청크는 섹션 위치를 그대로 씁니다. 여러 섹션을 합치면 처음 값을 두고 달라지는 키에 `<키>_end`를 더합니다(예: `{"page": 3, "page_end": 4}`, `{"table": 2, "row": 2, "row_end": 9}`). `lines`는 `[처음 줄, 마지막 줄]`로 합칩니다. 겹쳐 붙인 앞 청크 부분은 위치에 넣지 않습니다.
- 파일명 청크(`meta_only`, `no_text`, `unsupported_format`): `content`는 `파일명` 줄과 경로 줄, 위치와 제목은 비우고, 임베딩하지 않습니다(`embeddable=False`).
- `bm25_text`는 `content`를 6.4의 함수로 토큰화한 값입니다. 파일명과 제목은 BM25 색인의 `filename`, `heading_path` 컬럼이 맡습니다.

**임베딩 호출**

- 문서 청크는 `input_type=passage`, 질문은 `input_type=query`로 호출하고 `truncate=END`, `modality=text`, `encoding_format=float`, `dimensions=EMBED_DIM`을 씁니다(`input_type`·`truncate`·`modality`는 openai SDK의 `extra_body`). 응답 차원이 `EMBED_DIM`과 다르면 `EmbeddingDimensionError`로 멈춥니다.
- API 키는 모델마다 따로 발급되므로 `NVIDIA_LLM_API_KEY`, `NVIDIA_EMBED_API_KEY`, `NVIDIA_RERANK_API_KEY`를 각 Provider가 자기 것만 읽습니다. 예제처럼 `"$NVIDIA_API_KEY"` 문자열을 그대로 넣으면 인증이 실패합니다.
- 청크마다 `embed_model`을 기록하고, 문서에는 모델명과 차원을 기록합니다. 설정의 모델이 바뀌면 `backend reindex --embeddings` 대상이 됩니다.

**벡터 컬럼 타입 결정**

| 실제 임베딩 차원 | 컬럼 | 색인 |
| --- | --- | --- |
| 2,000 이하 | `vector(N)` | HNSW |
| 2,001\~4,000 | `halfvec(N)` | HNSW |
| 4,000 초과 | ① API가 차원 축소(`dimensions` 등)를 지원하면 2,048 등으로 줄여 `halfvec` 사용<br/>② 안 되면 `halfvec(N)`에 저장하고, 바이너리 양자화 식(`binary_quantize`)으로 HNSW 색인을 만들어 후보를 뽑은 뒤 원래 벡터로 다시 정렬 | ① 우선 |

1,000개 파일 규모면 청크가 수만 개 수준이라, 최악의 경우 색인 없이 전수 비교해도 검색 목표(p95 500ms)를 맞출 가능성이 있습니다. 구축 초기에 실제 차원을 확인하고 표에 따라 결정합니다.

### 6.4 한국어 BM25 토큰화

- Kiwi로 형태소를 분석해 명사(NNG, NNP, NNB), 동사·형용사 어간(VV, VA), 외국어(SL), 숫자(SN), 한자(SH), 어근(XR)만 남기고 조사와 어미는 버립니다.
- 영문은 소문자로 바꾸고, 복합명사는 원형과 분해형을 모두 넣습니다(예: "위약금조항" → `위약금조항 위약금 조항`). 복합명사는 띄어쓰기 없이 붙어 나온 일반·고유명사(NNG, NNP)의 연속으로 판단합니다. Kiwi 태그의 불규칙 활용 표시(`VA-I` 등)는 `-` 앞만 봅니다.
- Kiwi는 기본으로 URL, 이메일, 날짜·버전 같은 일련번호를 한 토큰(`W_URL`, `W_SERIAL` 등)으로 묶는데, 이 태그는 위 목록에 없어 통째로 버려집니다. 그래서 이 패턴 매칭을 끄고 SL·SN 조각으로 남깁니다(예: `2024-01-02` → `2024 01 02`).
- 구현: `backend/app/ingest/tokenize.py`의 `bm25_tokens`(목록), `bm25_text`(공백으로 이은 문자열). 청커와 질의 파이프라인(retrieve)이 모두 이 함수를 씁니다.
- 질문도 같은 함수로 토큰화합니다. 문서와 질문 토큰화가 어긋나면 BM25 품질이 바로 떨어지므로, 토큰화 함수는 하나만 둡니다.
- 사용자 사전(`deploy/kiwi_user_dict.txt`, 다른 파일은 `KIWI_USER_DICT_PATH`)으로 프로젝트명, 고유명사를 추가할 수 있게 합니다. 형식은 한 줄에 `단어<TAB>품사[<TAB>점수]`, `#`은 주석입니다. 사전을 바꾸면 `backend reindex --bm25`로 다시 색인합니다(API 호출 없음).
- 평가셋으로 pg\_search 내장 한국어 토크나이저와 비교해, 더 나은 쪽을 남깁니다(PRD 9장 리스크).

### 6.5 문서 삭제와 보관

| 원인 | 시점 | 처리 |
| --- | --- | --- |
| 루트 해제, 제외 패턴 추가 | `PUT /settings/sync` 저장 시 | 지금 설정으로 범위 안이던 문서 중 새 설정에 맞지 않는 문서를 즉시 영구 삭제(이미 범위 밖이던 문서는 대상 아님). 저장 전에 `POST /settings/sync/preview`로 삭제될 문서 수와 예시 경로를 보여주고 확인받음 |
| 로컬 삭제 | `deleted_at`부터 30일(`LOCAL_DELETED_RETENTION_DAYS`) | Worker의 APScheduler가 매일 04:00에 기간이 지난 문서를 영구 삭제 |
| 사용자 요청 | 문서 화면에서 "영구 삭제" | `DELETE /documents/{id}`로 즉시 삭제 |

- **영구 삭제 범위:** `documents`, `sections`, `chunks`, 관련 `jobs`, `tmp`의 임시 원본입니다.
- **예전 대화의 인용:** 범위 제외와 사용자 요청으로 삭제한 문서는 프라이버시 때문에 지운 것이므로, `messages.citations`의 인용 구절도 "삭제된 문서"로 바꿉니다. 로컬 삭제 30일 경과로 지운 문서는 인용을 그대로 둡니다.
- **질의 로그:** `query_logs.retrieved`에는 청크 ID와 점수만 있으므로 그대로 둡니다.
- **확인 문구:** 범위 제외와 사용자 삭제의 확인 창에 "백업 파일에는 최대 7일(PC)까지 남습니다. 이미 NVIDIA API로 보낸 텍스트는 되돌릴 수 없습니다."를 표시합니다.
- **로그:** 삭제는 `documents.purged` 구조화 로그 이벤트로 남깁니다(원인, 개수만, 경로 제외).
- **구현:** `backend/app/ingest/purge.py`의 `purge_documents(ids, reason)` 하나로 처리합니다(`reason`: `user`·`scope`·`retention`). `user`·`scope`는 인용을 가리고(`messages.citations`의 해당 문서 항목 `snippet`을 "삭제된 문서"로, `deleted: true` 추가. 번호·경로는 둠), `retention`은 그대로 둡니다. sections·chunks·jobs는 외래 키 `ON DELETE CASCADE`로 함께 지우고 임시 원본(`{id}-*`)도 지웁니다. 로그 필드: `reason`, `documents`, `sections`, `chunks`, `jobs`, `masked_messages`.
- **30일 자동 삭제 구현:** `backend/app/lifecycle/retention.py`의 `purge_expired(now, days)`가 `locally_deleted`이고 `deleted_at <= now - LOCAL_DELETED_RETENTION_DAYS`인 문서를 `reason=retention`으로 지웁니다(`deleted_at`이 없는 문서는 기간을 셀 수 없어 둡니다). Worker 예약 작업 ID `retention_purge`(매일 04:00 한국 시간), 수동 실행은 `rag-backend purge-retention`. 아무것도 지우지 않아도 `documents.purged`(`documents: 0`)를 남겨 작업이 돌았는지 확인할 수 있습니다.

## 7. 질의 파이프라인

### 7.1 LangGraph 그래프

```mermaid
flowchart TD
    START([질문]) --> SCOPE["scope<br/>필터 적용 문서 수 확인"]
    SCOPE -- "0개" --> LOG
    SCOPE --> REWRITE["rewrite<br/>독립 질문, 키워드 질의, 의도 분류"]
    REWRITE -- "intent=qa" --> RETRIEVE["retrieve<br/>질문 임베딩 + Hybrid SQL"]
    REWRITE -- "intent=summarize" --> SUMMARIZE["summarize<br/>파일 단위 요약"]
    RETRIEVE --> RERANK{"rerank<br/>(설정으로 켜고 끔)"}
    RERANK --> GRADE["grade<br/>근거 충분한가"]
    GRADE -- "충분" --> GENERATE["generate<br/>인용 번호 포함 답변 스트리밍"]
    GRADE -- "부족, 첫 시도" --> RETRY["retry_rewrite<br/>질의 바꾸기"] --> RETRIEVE
    GRADE -- "부족, 재시도 후" --> NOTFOUND["not_found<br/>찾지 못함 응답"]
    GENERATE --> LOG["log<br/>질의 로그 저장"]
    SUMMARIZE --> LOG
    NOTFOUND --> LOG
    LOG --> END([종료])
```

**그래프 상태(`QueryState`):** `question`, `history`(최근 6턴), `filters`, `rewritten`, `keyword_query`, `intent`, `target_document_id`, `candidates`, `selected`, `grade`, `attempt`, `answer`, `citations`, `timings`

구현(`backend/app/query/`): `graph.py`(그래프, 노드), `prompts.py`(프롬프트), `postprocess.py`(답변 후처리), `summary_target.py`(요약 대상 판정), `fake_llm.py`(Fake LLM 응답기).

- 그래프는 한 번 컴파일해 재사용하고, DB 세션과 Provider는 실행 컨텍스트(`context_schema`)로 넘깁니다. 단계별 지연·후보 점수·판정·모델명은 상태와 별도의 `QueryTrace`에 모읍니다. 중간에 끊기거나 실패해도 그때까지 모은 값으로 질의 로그를 남기기 위해서입니다(`timings`는 `QueryTrace.latency_ms`).
- `scope`는 그림의 노드 앞에 둔 범위 확인 단계입니다. 필터에 맞는 문서가 0개면 검색하지 않고 안내 문구로 끝냅니다(7.3 순서표대로 `status` 없이 `token` → `done`). 문서를 직접 고른 요약 요청(`summarize_document_id`)은 필터와 상관없이 진행합니다.
- 프롬프트의 시스템 메시지 첫 줄에 작업 표식(`[작업:rewrite]`, `grade`, `retry_rewrite`, `generate`, `summarize_map`, `summarize_reduce`)을 둡니다. 로그·디버깅에서 호출을 구분하고, Fake LLM 응답기도 이 표식으로 작업마다 결정적인 답을 만듭니다. 자료는 `<자료 번호="n" 파일="…" 위치="…" 수정일="YYYY-MM-DD">…</자료>`로 감싸고, 자료 안의 `<자료`·`</자료` 문자열은 바꿔 넣어 구분자를 깨지 못하게 합니다.
- 짧은 판단(rewrite, grade, retry\_rewrite)은 `temperature` 0, `max_tokens` 300의 JSON 응답입니다. rewrite·retry\_rewrite는 파싱에 실패하면 원래 질의를 쓰고, grade는 파싱에 실패하면 "충분"으로 보고 생성으로 넘깁니다(생성 단계가 근거 밖 답을 막고 "찾지 못했다"고 말하게 지시되어 있음). 이력이 없을 때의 의도는 질문에 "요약"(summar)이 있는지로 정합니다.
- 재정렬 뒤 상위 8개를 고를 때도 문서당 최대 3개를 지킵니다. 재정렬 호출이 실패하면 RRF 순서로 계속합니다.
- 요약은 문서의 sections(없으면 chunks)를 8,000자 부분으로 묶습니다. 부분이 1개면 바로 요약하고, 여러 개면 부분 요약(map) 후 합칩니다(reduce). 부분이 12개를 넘으면 이웃한 부분을 묶고 묶음마다 앞 8,000자만 씁니다(`query.summary.truncated` 로그).

| 노드 | 입력 → 출력 | 구현 메모 |
| --- | --- | --- |
| rewrite | 질문·이력 → 독립 질문, 키워드 질의, 의도 | 이력이 없으면 LLM을 부르지 않고 그대로 통과(지연 절약). 출력은 JSON으로 받고, 파싱 실패 시 원문 질문 사용 |
| retrieve | 독립 질문 → 후보 30개 | 질문 임베딩과 Kiwi 토큰을 만들어 7.2의 SQL 실행 |
| rerank | 후보 30개 → 상위 8개 | `RERANK_ENABLED=false`면 RRF 상위 8개를 그대로 사용. 사용 여부는 평가셋으로 결정 |
| grade | 상위 청크 → 충분·부족 | 1차는 LLM 판정(짧은 JSON 응답). 지연이 목표를 넘으면 재정렬 점수 임계값으로 바꿉니다 |
| retry\_rewrite | 판정 사유 → 새 질의 | 동의어, 상위 개념, 키워드 위주로 바꿉니다. 재검색은 1회만 |
| generate | 선택된 청크 → 답변 | 청크를 `[1]`\~`[k]`로 번호 붙여 넣고, 문장 끝마다 인용 번호를 달게 지시. 근거 밖 지식 사용 금지. 청크는 구분자로 감싸 "자료"로 넣고, 그 안의 지시문은 따르지 않게 지시. 근거끼리 내용이 다르면 각 출처의 수정일을 밝히게 지시. 답변은 질문과 같은 언어로 작성 |
| summarize | 대상 문서 → 요약 | 대상은 출처 패널의 "요약" 버튼이나 후보 목록에서 고른 문서(`summarize_document_id`, 명시적 선택이라 가장 앞), 질문에 나온 파일명(확장자 포함 이름, 없으면 확장자를 뺀 2자 이상 이름), 직전 답변이 인용한 문서("그 문서") 순으로 정합니다. 문서가 길면 sections를 묶어 부분 요약 후 합치기(map-reduce). 대상 판정 규칙은 아래 표 |
| not\_found | → 고정 응답 | "문서에서 찾지 못했습니다"와 함께 현재 검색 범위(`coverage`)와 가장 가까웠던 후보 3개를 참고로 보여줌 |
| log | 상태 → `query_logs` | 단계별 지연, 후보, 판정, 모델명 저장 |

**요약 대상 판정**

| 경우 | 응답 |
| --- | --- |
| 후보 1개, 상태 `indexed`·`embedding`·`ready` | 요약 진행(`embedding`도 sections가 있어 요약 가능) |
| 후보 여러 개 | 요약하지 않고 후보 목록(경로, 수정일)을 보여주며 고르게 함. 고르면 `summarize_document_id`로 다시 요청 |
| 후보 없음 | "해당 파일을 찾지 못했습니다"와 비슷한 파일명 3개(`difflib` 유사도). `done(not_found=true)` |
| 후보가 `queued`·`extracting` | "아직 처리 중입니다" |
| 후보가 `meta_only`·`no_text`·`failed` | 요약할 텍스트가 없는 이유(10MB 초과, 텍스트 없음, 실패 사유)를 안내 |

**검색 범위(`coverage`)**

질의 시점에 필터를 적용한 문서 수를 셉니다: `bm25_docs`(키워드 검색 가능), `vector_docs`(의미 검색 가능), `pending_docs`(대기, 처리 중), `filters_applied`. 구현(`app/query/search.py`의 `compute_coverage`) 기준은 다음과 같습니다.

| 값 | 기준 |
| --- | --- |
| `bm25_docs` | 청크가 1개 이상인 문서(파일명 청크만 있는 `meta_only`·`no_text`·미지원 형식 포함) |
| `vector_docs` | 임베딩된 청크가 1개 이상인 문서(`embedding` 중 일부만 끝난 문서 포함) |
| `pending_docs` | 상태가 `queued`, `extracting`, `embedding`인 문서(화면의 "대기"·"처리 중") |
| `total_docs` | 필터에 맞는 전체 문서 수. 0이면 검색 SQL을 실행하지 않음 |
| `filters_applied` | 기본값이 아닌 필터만 담은 객체(예: `{"directory": "C:\…", "exts": ["md"], "include_deleted": false}`) |
 필터에 맞는 문서가 0개면 검색하지 않고 "필터 조건에 맞는 문서가 없습니다"로 답합니다. 문서가 하나도 없으면 첫 실행 체크리스트로 안내합니다.

**답변 후처리**

- 존재하지 않는 인용 번호는 지웁니다.
- 인용이 하나도 없는 답변은 근거 충실성 지표에서 실패로 셉니다.
- 추론(thinking) 모델은 답 앞에 추론 텍스트(`reasoning_content`)를 냅니다. 끄는 키가 모델마다 달라(nemotron은 `enable_thinking`, deepseek은 `thinking`) `LLM_EXTRA_BODY`(JSON)를 요청 본문에 그대로 합칩니다. 기본값은 `{"chat_template_kwargs":{"enable_thinking":false}}`입니다. 그래도 추론 텍스트가 오면 답변과 저장에서 제외합니다.
- LLM 설정은 PRD 검토대로 `temperature` 0\~0.3, `max_tokens` 1,024\~2,048, `stream=True`로 둡니다.

### 7.2 Hybrid 검색 SQL

벡터 상위 50개와 BM25 상위 50개를 SQL 하나 안에서 RRF(k=60)로 합쳐 후보 30개를 고릅니다. 구현은 `backend/app/query/search.py`(`hybrid_search`, 질문 임베딩까지 하는 `search`)이고, 아래는 pg\_search 0.26.5 기준 실제 구조입니다.

```sql
-- 같은 트랜잭션에서 먼저: SET LOCAL hnsw.iterative_scan = relaxed_order; SET LOCAL hnsw.ef_search = 100;
--                        SET LOCAL paradedb.planner_warnings = 'Off';
WITH vec AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY dist, id) AS r
  FROM (
    SELECT c.id, c.embedding <=> CAST(:qvec AS halfvec) AS dist
    FROM chunks c JOIN documents d ON d.id = c.document_id
    WHERE c.embedding IS NOT NULL /* AND 필터 */
    ORDER BY c.embedding <=> CAST(:qvec AS halfvec)
    LIMIT 50
  ) v
),
kw AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY score DESC, id) AS r
  FROM (
    SELECT c.id, pdb.score(c.id) AS score
    FROM chunks c JOIN documents d ON d.id = c.document_id
    WHERE (c.bm25_text ||| :qtokens OR c.filename ||| :qtokens OR c.heading_path ||| :qtokens)
      /* AND 필터 */
    ORDER BY pdb.score(c.id) DESC
    LIMIT 50
  ) k
),
fused AS (
  SELECT id, vec.r AS vector_rank, kw.r AS bm25_rank,
         COALESCE(1.0 / (60 + vec.r), 0) + COALESCE(1.0 / (60 + kw.r), 0) AS rrf
  FROM vec FULL OUTER JOIN kw USING (id)
),
capped AS (
  SELECT f.*, c.document_id,
         ROW_NUMBER() OVER (PARTITION BY c.document_id ORDER BY f.rrf DESC, f.id) AS doc_rank
  FROM fused f JOIN chunks c ON c.id = f.id
)
SELECT … FROM capped x JOIN chunks c … JOIN documents d …
WHERE x.doc_rank <= 3
ORDER BY x.rrf DESC, x.id
LIMIT 30;
```

- **BM25 검색어:** 질문(과 rewrite의 `keyword_query`)을 문서와 같은 Kiwi 함수(`bm25_tokens`, 6.4)로 토큰화해 공백으로 잇습니다. `|||`(match disjunction)는 필드의 토크나이저로 검색어를 자른 뒤 OR로 찾으므로 `bm25_text`(공백)·`filename`·`heading_path`(simple)에 같은 문자열을 넘깁니다. 세 필드 점수는 `pdb.score`에서 합쳐집니다. `@@@ '문자열'`은 쿼리 파서를 거쳐 `:`·괄호·따옴표가 섞인 토큰에서 깨질 수 있어 쓰지 않습니다. 토큰이 없으면 `kw`를 비웁니다.
- **순위 계산 위치:** 각 목록은 안쪽 질의에서 `ORDER BY … LIMIT 50`으로 먼저 자르고 바깥에서 `ROW_NUMBER()`를 매깁니다. 창 함수를 `LIMIT`과 같은 단계에 두면 필터에 맞는 모든 행을 정렬하게 되어 HNSW 색인을 쓰지 못하고, `relaxed_order`로 조금 어긋난 순서도 바깥에서 바로잡습니다(청크 3,000개 실측에서 `Index Scan using ix_chunks_embedding_hnsw` + 필터 확인).
- **필터:** 디렉터리는 `starts_with(d.path_key, :prefix)`. 접두어는 입력을 `path_key`와 같이 NFC·소문자로 바꾸고 `/`를 `\`로, 끝에 `\`를 붙입니다(`C:\a`가 `C:\ab\…`에 걸리지 않게). `LIKE`는 `\`를 이스케이프 문자로 해석해 Windows 경로에서 틀리므로 쓰지 않습니다. 형식은 `ltrim(d.ext, '.') = ANY(:exts)`(입력은 점 없이 소문자), 수정일은 `d.mtime >= :from`, `d.mtime <= :to`(양 끝 포함). 로컬 삭제 문서는 기본으로 포함하고 결과에 `locally_deleted`로 표시만 하며, 필터의 `include_deleted=false`로 뺄 수 있습니다. 같은 필터를 검색 범위(7.1)에도 적용합니다.
- 아직 임베딩되지 않은 청크는 `vec`에 없고 `kw`로만 들어옵니다(PRD 5.2의 "BM25로만 검색"). 질문 임베딩 호출이 실패하면(한도 초과·장애) 질의를 막지 않고 `vec`를 비운 채 BM25로만 찾습니다(`search.embed_failed` 로그).
- 같은 문서의 청크가 상위를 독점하지 않도록 후보 30개를 고를 때 문서당 최대 3개로 제한합니다(`capped`). 재정렬 뒤 최종 8개를 고를 때도 같은 제한을 지킵니다.
- 필터에 맞는 문서가 0개(`total_docs = 0`)면 검색 SQL을 실행하지 않습니다.
- **결과 항목:** `chunk_id`, `document_id`, `path`, `filename`, `ext`, `location`, `heading_path`, `mtime`, `snippet`(본문에서 질문 토큰이 처음 나오는 곳 주변 200자), `content`, `locally_deleted`, `rrf_score`, `vector_rank`, `bm25_rank`(목록에 없으면 null), `rank`. 질의 로그 `retrieved`에는 ID·점수·순위만 넣습니다(`log_entry()`).
- **지연:** `latency_ms`에 `embed`(질문 임베딩), `coverage`, `sql`, `total`(ms)을 담아 돌려줍니다. 로그 이벤트 `search.completed`에 결과 수, 문서 수, 벡터·BM25 후보 수, 검색 범위 숫자, 적용한 필터 이름, 지연을 남깁니다(질문·경로는 남기지 않음).
- 개발·E2E 확인용 CLI: `rag-backend search "질문" [--dir 폴더] [--ext md] [--from 2026-10-01] [--to …] [--exclude-deleted] [--json]`.

### 7.3 SSE 이벤트

`POST /api/conversations/{id}/messages`의 응답은 `text/event-stream`입니다.

| 이벤트 | 데이터 | 시점 |
| --- | --- | --- |
| `status` | `{"stage": "rewriting" \| "searching" \| "reranking" \| "grading" \| "retrying" \| "summarizing" \| "generating", "progress"?: {"done", "total"}}` | 노드 시작 시. `progress`는 요약의 부분 요약 진행률에만 씀 |
| `sources` | `[{"n": 1, "chunk_id", "document_id", "path", "location", "mtime", "snippet", "locally_deleted"}]` | 생성 직전(출처 패널을 먼저 채움). 인용 번호가 없는 참고 후보·요약 후보는 `n: null`, 문서 단위 출처(요약)는 `chunk_id: null` |
| `token` | `{"text": "..."}` | 생성 중 |
| `done` | `{"message_id", "conversation_id", "not_found", "coverage", "latency_ms"}` | 완료. `latency_ms`는 전체 지연(정수 ms), 단계별 지연은 질의 로그에 |
| `error` | `{"code", "message", "retryable"}` | 실패 |

**경우별 이벤트 순서**

| 경우 | 순서 |
| --- | --- |
| 일반 답변 | `status`(여러 번) → `sources` → `token`(여러 번) → `done(not_found=false)` |
| 찾지 못함 | `status`(여러 번) → `sources`(가까운 후보 3개, 인용 번호 없이 참고용) → `token`(고정 문구) → `done(not_found=true, coverage)` |
| 필터 결과 0개, 문서 0개 | `token`(안내 문구) → `done(not_found=true, coverage)` |
| 요약 | `status(summarizing, progress)`(여러 번) → `sources`(대상 문서) → `token` → `done` |
| 요약 대상 여러 개 | `sources`(후보 문서들) → `token`(고르라는 안내) → `done` |
| 요약 대상이 처리 중·텍스트 없음 | `sources`(대상 문서) → `token`(이유 안내) → `done` |
| 요약 대상 없음 | `token`(비슷한 파일명 안내) → `done(not_found=true)` |
| 실패 | 어느 시점에서든 `error`로 끝남. 이미 보낸 토큰은 `status=error`로 저장 |

**오류 코드**

| `code` | 상황 | `retryable` | 화면 문구 |
| --- | --- | --- | --- |
| `rate_limited` | NVIDIA 429 재시도 초과 | true | 요청이 많아 잠시 막혔습니다. 잠시 후 다시 시도하세요 |
| `provider_unavailable` | 5xx, 연결 실패, 모델 종료 | true | 모델 서버에 연결하지 못했습니다 |
| `provider_auth` | API 키 오류, 크레딧 소진 | false | API 키 또는 사용량을 확인하세요(관리자 조치 필요) |
| `timeout` | 전체 60초 초과 | true | 응답이 너무 오래 걸렸습니다 |
| `conflict` | 같은 대화에서 이미 생성 중 | false | 이전 답변이 끝난 뒤 다시 보내세요 |
| `internal` | 그 밖의 서버 오류 | true | 문제가 발생했습니다 |

- 노드 이벤트와 토큰은 LangGraph의 스트리밍(`astream`의 custom 모드, 노드가 `get_stream_writer()`로 씀)으로 받아 SSE로 바꿉니다. LangChain 채팅 모델을 쓰지 않으므로 messages 모드는 쓰지 않습니다. 그래프는 별도 작업(task)에서 돌고 이벤트는 큐로 SSE에 넘깁니다.
- 경로의 `{id}`가 `new`면 대화를 새로 만듭니다(제목은 질문 앞 40자, 제목이 빈 기존 대화도 첫 질문으로 채움). 응답 헤더 `X-Conversation-Id`로 대화 ID를 바로 알려 주므로, 웹은 첫 이벤트 전에 URL을 `/c/{id}`로 바꿉니다. 숫자가 아니거나 없는 대화면 404 `conversation_not_found`. 같은 대화의 동시 요청은 대화 행 `FOR UPDATE`로 줄 세웁니다.
- 저장: 질문을 받으면 사용자 메시지(`complete`)와 답변 메시지(`streaming`)를 먼저 저장하고, 끝나면 답변과 `citations`(보낸 `sources` 그대로)를 `complete`로 바꿉니다. 실패·중단에도 질의 로그를 남깁니다.
- 질문 전체 시간 상한은 `CHAT_TIMEOUT_SECONDS`(60초)입니다. 오류 이벤트와 409 응답 본문에는 `retryable`이 함께 있습니다.
- 로그: `chat.message.received`(대화·메시지 ID, 길이, 이력 수, 필터 이름), `chat.message.completed`·`chat.message.not_found`(출처·인용 수, 판정, 재검색 여부, 검색 범위 숫자, 지연), `chat.message.failed`(`code`, 예외 이름), `chat.message.aborted`, `chat.message.conflict`, `chat.message.stale_aborted`. 질문·답변 원문은 남기지 않습니다(`LOG_QUERY_TEXT=true`일 때만 `received`에 질문).
- Nginx는 이 경로에서 `proxy_buffering off`를 적용하고, 응답에 `X-Accel-Buffering: no`를 붙입니다.
- 클라이언트가 연결을 끊으면 생성을 중단하고, 그때까지의 답변을 `status=aborted`로 저장합니다.
- 같은 대화에 `status=streaming`인 메시지가 있으면 새 질문은 SSE를 시작하지 않고 HTTP 409(`conflict`)로 거절합니다. 2분 넘게 `streaming`인 메시지는 `aborted`로 정리합니다.
- 질문 길이는 1\~4,000자입니다.

### 7.4 지연 시간 예산 (p95 8초)

| 단계 | 예산 |
| --- | --- |
| rewrite(이력 있을 때만) | 1.0초 |
| 질문 임베딩 | 0.5초 |
| Hybrid SQL | 0.3초 |
| rerank | 0.8초 |
| grade | 1.0초 |
| 첫 토큰까지 | 누적 3초 이내가 목표 |
| 생성 완료 | 나머지 |

NVIDIA API 실측값(13장)이 예산을 넘으면 rewrite 생략 조건을 넓히고, grade를 재정렬 점수 임계값으로 바꾸는 순서로 줄입니다.

## 8. API 명세

모든 경로는 `/api` 아래입니다. 요청과 응답은 JSON이고, 오류는 `{"error": {"code", "message"}}` 형식입니다. 웹용 API는 세션 쿠키를, Sync API는 Bearer 토큰을 씁니다.

### 8.1 인증 (세션)

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| POST | `/auth/login` | 아이디·비밀번호 → 세션 쿠키 발급. 실패는 401 `invalid_credentials`. 연속 5회 실패 시 15분 잠금. 잠금 중에는 429 `login_locked`와 남은 시간(`retry_after_s`, `Retry-After` 헤더)을 돌려줌(맞는 비밀번호여도) |
| POST | `/auth/logout` | 세션 삭제 |
| GET | `/auth/me` | 로그인 상태 확인 |

- 세션이 없거나 만료되면 401 `unauthenticated`, CSRF 검사에 걸리면 403 `csrf_failed`, Sync API 토큰이 없거나 폐기됐으면 401 `invalid_token`.
- 로컬 HTTP(개발·E2E)에서만 `SESSION_COOKIE_SECURE=false`로 쿠키의 `Secure`를 끌 수 있습니다. `APP_ENV=prod`에서는 설정 자체를 거부합니다.
- CSRF `Origin` 검사는 Origin의 호스트:포트가 요청 `Host`와 같아야 통과합니다. 프록시가 `Host`를 바꾸면 `CSRF_TRUSTED_ORIGINS`에 출처를 적습니다.

### 8.2 Sync API (Bearer 토큰, Agent 전용)

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/sync/config` | 루트, 제외 패턴, 최대 크기 |
| POST | `/sync/runs` | 실행 시작 기록(`source`) → `run_id` |
| POST | `/sync/runs/{id}/manifest` | 전체 목록과 `scan_errors` → `upload`, `meta_only`, `deleted`, `restored`, `moved`, `copied`, `deletion_held`(대량 삭제 보호로 보류) 목록 |
| POST | `/sync/runs/{id}/heartbeat` | 실행 중 1분마다 호출 |
| PUT | `/sync/files` | multipart 업로드(`path`, `sha256`, `mtime`, `file`). 서버가 해시를 다시 계산해 검증하고, 다르면 409 `hash_mismatch` |
| POST | `/sync/files/meta` | 10MB 초과 파일의 경로·크기·수정 시각 |
| POST | `/sync/runs/{id}/complete` | 실행 종료와 통계 |
| GET | `/sync/backups/latest` | 최신 백업 목록(파일명, 크기, SHA-256) |
| GET | `/sync/backups/{name}` | 백업 파일 다운로드 |

- 응답 세부(구현: `backend/app/api/sync.py`):
  - `GET /sync/config`: `allowed_root`, `roots`, `excludes`(`[{pattern, fixed}]`, 고정 제외가 `fixed: true`로 앞에 옴), `max_file_mb`, `extensions`.
  - `POST /sync/runs`: 201, `{run_id, status, started_at, heartbeat_at, finished_at}`. 하트비트·완료도 같은 형식.
  - manifest 요청: `{files: [{path, size, mtime, sha256}], scan_errors: [경로]}`. 응답: 경로 목록 `upload`, `meta_only`, `deleted`, `restored`, `deletion_held`, `scan_error_held`(읽지 못한 폴더 아래라 보류), `{from, to}` 목록 `moved`, `copied`, `rejected`(`[{path, reason}]`), `counts`(종류별 건수와 `unchanged`).
  - 완료 요청: `{status: succeeded|failed, stats, error?}`. `stats`는 그대로 저장하되 서버가 판정 때 남긴 `manifest`, `deletion_held` 키는 덮지 않습니다.
  - `POST /sync/files/meta` 요청: `{files: [{path, size, mtime, sha256}]}`. 응답: `accepted`(`[{path, document_id, outcome}]`), `rejected`(`max_file_mb` 이하면 `not_over_limit`).
  - 업로드 요청은 선택 필드 `run_id`를, 메타 등록 요청은 선택 키 `run_id`를 받습니다. 로그 상관 키로만 씁니다.
  - `GET /sync/backups/latest`: `{backups: [{name, size, sha256, created_at}]}`(VM에 있는 백업 전부, 최신 먼저. 이름 규칙 `rag-YYYYMMDD.dump.age`에 맞는 일반 파일만, 심볼릭 링크 제외). SHA-256은 백업을 만들 때 쓴 옆 파일 `<이름>.sha256`을 읽고, 없으면 계산해 다시 씁니다.
  - `GET /sync/backups/{name}`: 파일 그대로(`application/octet-stream`). 이름이 규칙에 맞고 실제 경로가 백업 폴더 바로 아래일 때만 내려주고, 그 밖(`..`, `/`·`\\`·인코딩된 구분자, 다른 파일, 폴더 밖을 가리키는 링크)은 모두 404 `backup_not_found`.
  - 업로드 응답: `{document_id, outcome: queued|unchanged}`. 받는 중에는 `UPLOAD_TMP_DIR`의 임시 파일(`incoming-*.part`)에 쓰고, 검증이 끝나면 `{document_id}-{sha256}{ext}`로 옮겨 `extract` 작업을 등록합니다.
- 오류 코드: 404 `backup_not_found`, 404 `run_not_found`, 409 `run_finished`(끝난 실행), 400 `outside_root`·`fixed_exclude`·`excluded`·`relative_path`·`unsupported_extension`·`invalid_sha256`, 413 `file_too_large`, 409 `hash_mismatch`.
- 로그(경로·파일명 없이 건수와 ID만, 업로드 관련 로그는 `run_id`와 해시 앞 8자리 `sha256_prefix`를 함께 남김): `sync.run.started`, `sync.manifest.judged`, `sync.deletion.held`(`reason`: `mass_delete_guard`·`scan_error`), `sync.file.uploaded`, `sync.file.rejected`, `sync.file.hash_mismatch`, `sync.file.meta_registered`, `sync.run.completed`, `sync.run.abandoned`, `sync.run.resumed`, `backup.listed`(`count`), `backup.downloaded`(`name`, `size`), `backup.download.rejected`.

### 8.3 웹 API (세션)

| 메서드 | 경로 | 설명 | 기능 |
| --- | --- | --- | --- |
| GET·PUT | `/settings/sync` | 루트, 제외 패턴 조회·수정. 루트가 `C:\Chanjin\dev` 하위가 아니면 400. 저장 시 범위에서 빠진 문서를 영구 삭제(6.5) | F-01 |
| POST | `/settings/sync/preview` | 바꿀 설정 → 범위에서 빠질 문서 수와 예시 경로 10개(저장하지 않음) | F-01, F-19 |
| GET | `/sync/status` | 최근 실행, 대기열 수, 오늘 임베딩 사용량(x/100), 최근 백업 시각, 보류된 삭제 판정, 건너뛴 형식 통계 | F-03, F-05 |
| POST | `/sync/held-deletions/apply` | 대량 삭제 보호로 보류된 판정을 다음 실행에서 적용 | F-02 |
| POST | `/sync/run-requests` | "지금 동기화" 클릭 기록(로그만, 실행은 Agent) | F-03 |
| GET | `/index/coverage` | 상태별 문서 수, 의미 검색 가능 수, 완료 예상일(대기 파일 수 ÷ 하루 한도) | F-18 |
| GET | `/setup/status` | 첫 실행 체크리스트: 사용자 존재, 토큰 발급 여부, Agent 첫 접속 시각, 첫 동기화 완료 여부, 문서 수 | F-18 |
| GET | `/documents` | 목록(상태, 형식, 경로, 검색어, 로컬 삭제 여부 필터, 페이지네이션) | F-05 |
| GET | `/documents/{id}` | 메타데이터와 위치별 추출 텍스트 | F-10 |
| POST | `/documents/{id}/reprocess` | 실패 파일은 `needs_upload` 표시, 정상 파일은 sections에서 재청킹 | F-05 |
| DELETE | `/documents/{id}` | 영구 삭제(6.5) | F-19 |
| GET | `/documents/suggest` | 경로 앞부분 → 폴더 경로 후보(디렉터리 필터 자동완성) | F-07 |
| GET·POST | `/conversations` | 대화 목록·생성 | F-11 |
| GET·PATCH·DELETE | `/conversations/{id}` | 대화와 메시지 조회, 제목 변경, 삭제 | F-11 |
| POST | `/conversations/{id}/messages` | 질문(`text`, `filters`, `summarize_document_id?`) → SSE 응답. 생성 중이면 409. `{id}`가 `new`면 대화를 만듦. `filters`: `directory`, `exts`, `modified_from`·`modified_to`(날짜, 한국 시간 그날 시작·끝), `include_deleted` | F-07\~F-09 |
| PUT | `/messages/{id}/feedback` | 좋아요·싫어요, 사유, 메모(다시 보내면 덮어씀). 실패 유형은 검토 화면에서 따로 붙임 | F-12 |
| GET | `/eval/runs`, `/eval/runs/{id}` | 평가 실행 목록, 지표, 이전 실행과 비교 | F-13 |
| GET·PATCH | `/feedback`, `/feedback/{id}` | 피드백 목록(`rating=-1`이면 싫어요만), 실패 유형 붙이기(`reviewed_at` 기록) | F-12 |
| GET | `/health` | 공개 요약(인증 불필요): `status`, `degraded`, `issues[].code` | 운영 |
| GET | `/health/details` | 세부(세션): DB 연결, Worker 마지막 하트비트, 디스크 여유, NVIDIA API 마지막 성공·실패 시각과 오류 코드, 배너용 이상 목록 | 운영 |

- 인증이 필요한 API는 세션이 없거나 만료되면 401을 돌려줍니다. 웹은 입력 중인 질문을 보존한 채 로그인 화면으로 보내고, 로그인 후 원래 화면으로 돌아옵니다.
- 대화(`app/api/conversations.py`): 목록은 `updated_at` 내림차순 `[{id, title, created_at, updated_at}]`. `POST /conversations`(`title`?)는 제목을 주지 않으면 빈 제목으로 만들고 첫 질문 앞 40자로 채웁니다(웹은 빈 제목을 "새 대화"로 표시). `GET /conversations/{id}`는 메시지마다 `id`, `role`, `content`, `status`, `citations`, `created_at`, `not_found`·`coverage`(질의 로그에서), `feedback`(`rating`, `reason`, `note`)을 줍니다. `PATCH`는 `title`(공백 정리 후 1\~200자, 빈 값 422). `DELETE`는 204이며 메시지·질의 로그·피드백을 외래 키 `ON DELETE CASCADE`로 함께 지웁니다. 숫자가 아니거나 없는 대화는 404 `conversation_not_found`. 로그: `conversation.created`(`source`: `message`·`api`), `conversation.renamed`(제목 글자 수), `conversation.deleted`(지운 메시지·질의 로그·피드백 수). 제목 원문은 로그에 남기지 않습니다.
- 피드백(`app/api/feedback.py`): 본문 `{rating: 1 | -1, reason?, note?(2,000자 이하, 빈 문자열은 NULL)}`. `message_id` 기준 `INSERT … ON CONFLICT DO UPDATE`로 덮어쓰며 `failure_type`·`reviewed_at`은 그대로 둡니다. 답변이 아닌 메시지·없는 메시지는 404 `message_not_found`. 로그 `feedback.saved`(메시지 ID, 평가, 사유, 메모 유무. 메모 원문은 남기지 않음).
- 피드백 검토(`app/api/feedback.py`): `GET /feedback?rating=`(1·-1, 그 밖은 422)는 검토 안 한 것 먼저, 최신순 `{items, total, unreviewed}`. 항목은 `id`, `message_id`, `conversation_id`, `rating`, `reason`, `note`, `failure_type`, `reviewed_at`, `created_at`, `updated_at`, `question`(바로 앞 사용자 메시지), `answer`. `PATCH /feedback/{id}`(`{failure_type}`, null이면 분류 해제)는 `reviewed_at`을 지금으로 바꿈. 없는 피드백은 404 `feedback_not_found`. 로그 `feedback.listed`, `feedback.reviewed`(피드백·메시지 ID, 평가, 유형, 이전 유형).
- 평가(`app/api/eval.py`): `GET /eval/runs`는 `{metrics(지표 정의: key, label, target, higher_is_better), items}`(최신순). 실행마다 `id`, `dataset_version`, `git_sha`, `judge_model`, `llm_model`, `started_at`, `finished_at`, `metrics`, `previous_id`·`previous_metrics`(id가 더 작은 완료된 실행 중 가장 최근), `deltas`(지표별 현재 − 이전, 한쪽이 없으면 null). `GET /eval/runs/{id}`는 여기에 `config`, `results`(`item_id`, `answer`, `retrieved`, `scores`)를 더하고, 없으면 404 `eval_run_not_found`. 로그 `eval.runs.listed`, `eval.run.read`.
- 문서(`app/api/documents.py`): `GET /documents`는 `status`(여러 번 가능), `ext`(점 없이도 됨), `path`(폴더 앞부분, 폴더 경계로 자르고 대소문자 무시), `q`(경로·파일명 부분 일치, 대소문자 무시), `locally_deleted`, `page`(1부터), `page_size`(기본 50, 최대 200)를 받고 수정 시각 최신순 `{items, total, page, page_size, counts}`를 줍니다. `counts`는 필터와 무관한 상태별 개수와 `locally_deleted` 개수(화면 탭 숫자). 항목: `id`, `path`, `filename`, `ext`, `size`, `mtime`, `status`, `failure_code`, `failure_detail`, `locally_deleted`, `deleted_at`, `needs_upload`, `chunks`(청크 수), `updated_at`. `GET /documents/{id}`는 여기에 `sections`(`seq`, `location`, `heading_path`, `kind`, `text`)를 더합니다. `POST /documents/{id}/reprocess`: `failed`·`no_text`(또는 섹션이 없는 `ready`·`indexed`)는 `needs_upload=true`만 표시(상태·사유는 그대로, 다시 올라오면 새로 판정) → `action: needs_upload`. `ready`·`indexed`는 기다리는 `index`·`embed` 작업을 `done`으로 정리하고 상태를 `extracting`(처리 중)으로 바꾼 뒤 `index` 작업을 등록 → Worker가 sections에서 재청킹(같은 임베딩 입력이면 벡터 재사용, 같은 버전이라 한도도 다시 차감하지 않음) → `action: rechunk`. 그 밖의 상태(대기·처리 중·파일명만)는 409 `not_reprocessable`. `DELETE`는 204(6.5 구현). `GET /documents/suggest?prefix=`는 앞부분 바로 다음 단계까지의 폴더 경로(원래 표기, 최대 20개). 숫자가 아니거나 없는 문서는 404 `document_not_found`. 로그: `documents.listed`(쓴 필터 이름과 개수, 검색어 원문 없음), `documents.read`, `documents.suggested`, `documents.reprocess.requested`(문서 ID, 이전 상태, 방식, 실패 코드), `documents.purged`.
- 동기화 범위 설정(`app/api/settings.py`, `app/sync/settings.py`): `GET`은 `allowed_root`, `roots`, `excludes`(`[{pattern, fixed}]`, 고정 제외가 `fixed: true`로 앞에), `max_file_mb`, `updated_at`. 미리보기·저장 본문은 `{roots, excludes}`(편집 가능한 패턴만. 고정 제외는 대소문자·구분자를 무시하고 걸러 저장하지 않음). 루트는 공백·끝 구분자를 정리하고 `path_key`가 같은 것은 하나만 남기며, 허용 폴더 하위가 아니면 400 `outside_root`(`..` 포함), 상대 경로면 `relative_path`, 고정 제외 아래면 `fixed_exclude`. 미리보기는 `{count, examples(경로 소문자순 10개), roots, excludes}`. 저장은 선택 값 `expected_purge`(확인 창의 수)가 실제 삭제 수와 다르면 409 `preview_outdated`(`count` 포함, 웹은 다시 미리보기)이고, 성공하면 설정 응답 + `purged`. 로그: `settings.sync.read`, `settings.sync.previewed`(삭제 수, 루트·패턴 수), `settings.sync.updated`(루트·패턴 수와 추가·제거 수, `purged`, `masked_messages`), `settings.sync.rejected`(이유), `documents.purged`(`reason: scope`). 경로·패턴 원문은 남기지 않습니다.
- 동기화 상태(`app/api/sync_web.py`, 웹 세션. Agent용 `/sync/*`와 경로가 겹치지 않음): `GET /sync/status`는 `now`, `running`(진행 중 실행), `latest_run`, `last_success_at`(실행 요약: `id`, `source`, `status`, `started_at`, `heartbeat_at`, `finished_at`, `counts`(manifest 판정 건수·업로드 수), `error`), `queue`(`queued`, `running`, `by_type`), `embed`(`used`, `limit`), `last_backup_at`·`last_backup_name`(백업 폴더 `BACKUP_DIR`에서 가장 새 `rag-YYYYMMDD.dump.age`의 수정 시각·파일명, 없으면 null), `held_deletions`(판정을 받은 가장 최근 실행의 `stats.deletion_held.mass_delete`: `run_id`, `count`, `total`, `ratio`, `threshold`, `sample` 10개, `approved_at`), `skipped_formats`(마지막 성공 실행), `stale`(`sync`·`backup`: 없거나 24시간 넘음, 서버 시각 기준). `POST /sync/held-deletions/apply`(`{run_id?}`)는 승인을 기록하고 `{run_id, count, approved_at}`, 보류가 없으면 409 `no_held_deletions`, 화면의 실행과 다르면 409 `held_deletions_outdated`. `POST /sync/run-requests`(202)는 웹의 "지금 동기화" 클릭을 로그로만 남깁니다(`sync.run.requested`). 상태 조회 로그 `sync.status.read`는 2초 간격 조회라 debug 수준입니다.
- 첫 실행 체크리스트(`app/api/setup.py`): `GET /setup/status`는 `user_exists`, `token_issued`(폐기되지 않은 토큰), `agent_first_seen_at`(`api_tokens.first_used_at`의 최솟값. Bearer 인증이 처음 성공할 때 기록), `first_sync_completed`·`first_sync_at`(`succeeded` 실행), `documents`, `steps`(`key`: `user`·`token`·`agent`·`first_sync`·`documents`, `title`, `command`, `description`, `done`, `done_at`), `completed`, `total`, `complete`. 로그 `setup.status.checked`(완료 단계 수, 남은 단계 키, 문서 수).
- 상태 점검(`app/api/health.py`): 공개 `GET /health`는 프로세스가 살아 있으면 항상 200과 `{status: "ok", degraded, issues: [{code}]}`만 줍니다(Compose·E2E 준비 확인. 모델 이름·디스크 크기 같은 세부는 주지 않음). 세션이 필요한 `GET /health/details`(웹 전역 배너)는 `status`, `degraded`, `now`, `issues`(`{code, message, since, level}`, 코드 `db_unavailable`·`worker_stale`·`worker_missing`·`disk_low`·`provider_auth`), `checks`를 줍니다. `checks`: `db`(`SELECT 1`), `worker`(가장 최근 하트비트. `WORKER_STALE_SECONDS`(90) 넘으면 `stale`, 행이 없으면 `missing`), `disk`(`DISK_CHECK_PATH`, 기본 업로드 임시 폴더의 디스크. `DISK_MIN_FREE_GB`(10) 미만이면 `low`, 확인하지 못하면 `unknown`), `providers`(`provider_status` 행마다 마지막 성공·실패 시각, 오류 코드, 상태 코드, `failing`=마지막 실패가 마지막 성공보다 나중, `failing_since`). `provider_auth` 이상은 `failing`이고 코드가 `provider_auth`인 종류가 있을 때이며, 키 오류(401·403)와 크레딧·사용량 소진(402, `ProviderQuotaExhausted`)을 함께 다룹니다(402면 문구가 "크레딧·사용량 소진"). `since`는 같은 이상을 구분하는 시각입니다: `provider_auth`는 연속 실패의 첫 시각(실패가 이어져도 그대로라 배너가 실패마다 다시 뜨지 않음), `worker_stale`은 마지막 하트비트, 그 밖은 API 프로세스가 처음 발견한 시각. `level`은 이상이 심해지면 바뀌는 값(`disk_low`: 남은 GB 내림). 로그 `health.degraded`(이상 코드, Worker·디스크 상태, 실패 중인 Provider)는 이상의 내용(코드·`since`·`level`)이 바뀔 때만, 정상으로 돌아오면 `health.recovered`. 발견 시각과 마지막 로그 기록은 프로세스 메모리에 두므로 API를 여러 프로세스로 띄우면 프로세스마다 따로 기록됩니다(운영은 프로세스 하나).
- Provider 상태 기록(`app/providers/monitor.py`): 채팅(`_default_providers`)과 Worker·`ingest-local`의 Provider를 감싸 호출 결과를 `provider_status`에 남깁니다. 실패는 매번(`provider.failure.recorded` 로그, 같은 코드로 이어진 실패면 `failing_since`를 그대로 둠), 성공은 종류마다 60초에 한 번(직전이 실패였으면 바로, `failing_since`를 비움). 프로세스 시작 시 `configure_recorder(sessionmaker)`로 켜며(API lifespan, CLI), 켜지 않으면 기록하지 않습니다(pytest 기본). 기록 실패는 모델 호출에 영향을 주지 않습니다(`provider.status.record_failed`).
- 색인 범위(`app/api/index.py`): `by_status`(상태별 문서 수), `total_docs`·`bm25_docs`·`vector_docs`·`pending_docs`(질의의 검색 범위와 같은 기준), `embed_waiting`(`queued`·`extracting`·`indexed`·`embedding` 문서 수), `daily_limit`, `used_today`, `estimated_completion`(오늘 남은 한도를 먼저 쓰고 나머지를 하루 한도로 나눈 날 수를 더한 한국 시간 날짜, 기다리는 파일이 없으면 null). 로그 `index.coverage.read`.

FastAPI의 OpenAPI 스키마에서 `openapi-typescript`로 웹의 타입을 생성합니다. API를 바꾸면 웹 타입도 같이 갱신합니다.

## 9. 프론트엔드

### 9.1 화면

| 경로 | 화면 | 주요 요소 |
| --- | --- | --- |
| `/login` | 로그인 | 잠금 시 남은 시간 표시, "비밀번호를 잊었으면 서버에서 `rag-backend reset-password` 실행" 안내 |
| `/` , `/c/:id` | 채팅 | 왼쪽 대화 목록(이름 변경, 삭제), 가운데 메시지와 입력창(위에 디렉터리·형식·수정일·로컬 삭제 포함 필터), 오른쪽 출처 패널(접기 가능). 입력창 위에 색인 범위 한 줄(예: "키워드 검색 1,000 · 의미 검색 340 · 완료 예상 11월 14일"). 문서가 0개면 빈 화면 대신 첫 실행 체크리스트 |
| `/documents` | 문서 | 상태별 탭(대기, 처리 중, BM25만, 완료, 텍스트 없음, 실패, 파일명만, 로컬 삭제), 실패 사유와 해결 안내(3.4 표), 재처리 버튼(누르면 "다음 동기화 대기"와 "지금 동기화" 버튼 표시), 영구 삭제 버튼 |
| `/documents/:id` | 문서 상세 | 위치별 추출 텍스트, 인용 위치로 스크롤·강조, "이 문서 요약" 버튼. 문서가 삭제됐거나 인용 이후 바뀌었으면 "원문이 바뀌었거나 삭제되었습니다"와 저장된 인용 구절 표시 |
| `/sync` | 동기화 | 최근 실행, 대기열, 오늘 임베딩 사용량, 최근 백업, "지금 동기화" 버튼, 루트·제외 패턴 편집(저장 전 삭제될 문서 수 확인 창), 고정 제외는 읽기 전용 표시, 보류된 삭제 판정 경고, 건너뛴 형식 통계, 마지막 동기화·백업이 하루 넘으면 경고 배지 |
| `/eval` | 평가 | 실행 목록, 지표 표, 이전 실행 대비 증감, 싫어요 피드백 검토(실패 유형 붙이기) |

**공통 화면 상태**

- NVIDIA API가 `provider_auth` 오류를 내거나 `/health`가 Worker 하트비트 끊김을 보고하면 모든 화면 위에 배너를 띄웁니다.
- SSE `error`는 답변 말풍선 안에 화면 문구(7.3 표)를 보여주고, `retryable=true`면 "다시 시도" 버튼을 둡니다.
- 시각은 한국 시간으로 보여주고, 하루 이내는 "3시간 전"처럼 상대 시각을 씁니다.
- 너비 768px 미만에서는 대화 목록과 출처 패널을 서랍(drawer)으로 접습니다. Windows가 아닌 기기에서는 "지금 동기화"와 "폴더에서 보기"를 숨깁니다.

### 9.2 구조와 규칙

```
web/src/
├─ api/          # fetch 래퍼, 생성된 OpenAPI 타입, SSE 파서
├─ components/   # shadcn/ui 컴포넌트와 공용 컴포넌트
├─ features/     # chat/, documents/, sync/, eval/
├─ hooks/
├─ lib/
└─ routes/
```

- 서버 상태는 TanStack Query로만 관리하고, 전역 상태 라이브러리는 두지 않습니다. 필터와 패널 열림 같은 화면 상태는 URL 쿼리와 컴포넌트 상태로 처리합니다.
- **SSE 수신:** `EventSource`는 POST를 보낼 수 없으므로, `fetch`의 `ReadableStream`을 읽어 `event:`/`data:` 줄을 직접 파싱합니다. `AbortController`로 생성 중지를 지원합니다.
- **답변 렌더링:** react-markdown + remark-gfm으로 렌더링하고, `[n]`은 인용 칩으로 바꿉니다. 코드 블록 안의 `[n]`은 바꾸지 않습니다. 칩을 누르면 출처 패널의 해당 항목이 열립니다. 원시 HTML 렌더링은 끕니다. 이미지(`img`)는 렌더링하지 않고 대체 텍스트만 보여주며, 링크는 주소를 텍스트로만 표시합니다. 모델이 만든 외부 주소로 브라우저가 요청을 보내 문서 내용이 새는 것을 막기 위해서입니다(11장 CSP와 함께).
- **API 호출:** 모든 요청은 `credentials: "include"`로 보냅니다. GET이 아닌 요청에는 `X-Requested-With: fetch` 헤더를 붙입니다(CSRF 방어, 11장).
- **로컬 경로 표시:** 출처의 `C:\…` 경로는 복사 버튼을 제공합니다. 브라우저가 로컬 파일을 직접 열 수는 없으므로 링크로 만들지 않고, Windows에서는 "폴더에서 보기" 버튼으로 `chanjin-sync://reveal?k=<path_key의 SHA-256>`을 엽니다(10.2).
- **입력 보존:** 보내지 못한 질문(401, 네트워크 오류)은 대화별로 `sessionStorage`에 남겨 다시 열면 채웁니다.
- **접근성:** 스트리밍 답변 영역은 `aria-live="polite"`, 인용 칩은 버튼 요소로 만들어 키보드로 조작할 수 있게 합니다. 한글 조합 중 Enter는 전송하지 않습니다.
- **문서 화면 구현(`web/src/features/documents/`):** `DocumentsPage`(탭·검색어·폴더·쪽을 URL 쿼리로. 탭 9개 = 전체 + 9.1의 8개. 실패 사유·해결 안내는 `status.ts`의 3.4 표 문구. 재처리 결과 `needs_upload`면 "다음 동기화 대기"와 "지금 동기화"(동기화 화면으로 이동) 버튼, `rechunk`면 재청킹 안내. 영구 삭제 확인 창은 `alertdialog`로 6.5 확인 문구 표시), `DocumentDetailPage`(섹션별 추출 텍스트. `?loc=`의 위치에 드는 섹션을 강조하고 첫 섹션으로 스크롤. 청크 위치는 섹션 위치를 합친 것이라 `<키>_end` 범위·`lines` 겹침으로 판정. "이 문서 요약"은 `/`로 `state.summarize`를 넘겨 채팅이 `summarize_document_id`로 한 번 보냄. 문서가 404거나 저장된 인용의 수정 시각이 문서와 다르면·인용이 가려졌으면 "원문이 바뀌었거나 삭제되었습니다"와 저장된 인용 구절 표시). 채팅 디렉터리 필터는 `/documents/suggest` 결과를 `datalist`로 자동완성합니다.
- **동기화 화면 구현(`web/src/features/sync/SyncPage.tsx`, `src/api/sync.ts`):** 상태 카드(최근 실행·대기열·오늘 임베딩 x/한도·최근 백업·건너뛴 형식), 제목 옆 경고 배지(`stale`), 보류된 삭제 판정 경고(예시 경로, "삭제 판정 적용" → 확인 창 → 승인 후 "다음 동기화 때 적용" 표시), 수집 범위 편집(루트·편집 가능한 제외 패턴 목록, 고정 제외는 "고정 · 편집 불가"로 삭제 버튼 없이 표시하고 같은 패턴 추가를 막음. "저장"은 먼저 미리보기를 불러 확인 창에 삭제 수·예시·6.5 확인 문구를 보이고, 확인하면 그 수를 `expected_purge`로 저장). "지금 동기화"는 9.3 흐름: 누른 시점의 최근 실행 ID보다 큰 실행이 생기면 "새 실행"으로 봅니다(브라우저 시계와 서버 시계를 비교하지 않음). Windows 판정은 `userAgentData.platform`(없으면 `userAgent`)에 `Win`이 있는지입니다.
- **공통 화면 상태 구현:** 시각 표시는 `src/lib/time.ts`(`formatRelative`·`formatRelativeDateTime`·`formatDate`·`formatDateTime`, 한국 시간, 하루 이내 상대 시각)를 모든 화면이 함께 씁니다. 전역 배너 `src/components/GlobalBanner.tsx`는 `AppLayout` 머리글 아래에서 `/health/details`를 30초마다(채팅 답변이 끝날 때도) 읽어 `issues`를 `role="alert"`로 보여줍니다. 닫은 배너는 `sessionStorage`에 `code|since|level`(`src/lib/banner.ts`)로 기억하고, 이상이 사라졌다가 다시 생기거나 `since`·`level`이 바뀌면 다시 보여줍니다. 체크리스트 상태를 읽는 동안은 "불러오는 중", 읽지 못하면 기존 질문 안내와 확인 실패 문구를 보여줍니다. 시각·배너 키는 `web/tests/*.test.ts`(`corepack pnpm test`, Node 내장 테스트 러너)로 단위 테스트합니다. 첫 실행 체크리스트 `src/features/setup/SetupChecklist.tsx`는 `/setup/status`의 `documents`가 0이고 메시지가 없을 때 채팅 화면의 빈 안내 대신 단계별 명령과 완료 표시(완료 시각)를 보여줍니다.
- **채팅 화면 구현(`web/src/features/chat/`):** `ChatPage`(배치, 대화 불러오기, 768px 미만 서랍), `ConversationList`(이름 변경·삭제 확인), `AnswerMarkdown`(마크다운, 인용 칩. `[n]`은 remark 플러그인이 mdast의 text 노드에서만 바꾸므로 `code`·`inlineCode` 안은 그대로), `SourcePanel`(출처 항목, 문서 상세 링크 `/documents/{id}?loc=<위치 JSON>`, 저장된 인용을 화면 상태로 함께 넘김), `FeedbackBar`, `ChatFilters`. 다시 시도는 실패한 질문·답변 말풍선을 지우고 같은 질문·필터를 같은 대화로 다시 보냅니다(실패한 메시지는 DB에 `error`로 남음). "폴더에서 보기"의 `k`는 브라우저가 `crypto.subtle`로 계산합니다(path\_key 식은 10.2와 같음).

### 9.3 "지금 동기화" 버튼

1. 누르기 전에 `/sync/status`에 `running`인 실행이 있으면 버튼 대신 그 실행의 진행 상태를 보여줍니다(뮤텍스 때문에 새 실행이 바로 종료되므로).
2. 버튼을 누르면 `chanjin-sync://run?source=web`으로 이동하고(숨긴 `<a>` 클릭), 버튼을 60초 동안 비활성화합니다(Agent의 1분 반복 호출 제한과 맞춤).
3. 처음에는 브라우저가 "외부 프로그램 열기" 확인을 띄웁니다. "항상 허용"을 체크하면 이후에는 바로 실행됩니다.
4. 웹은 2초 간격으로 `/sync/status`를 조회합니다. 30초 안에 새 실행(출처와 무관, 누른 시각 이후 시작)이 생기면 진행 상태를 보여줍니다.
5. 실행이 생기지 않으면 "Agent가 설치되지 않았거나 브라우저가 실행을 막았습니다"라는 안내와 CLI 명령(`rag-agent sync`)을 보여줍니다.

## 10. Sync Agent

### 10.1 설치와 실행

`rag-agent install` 명령 하나로 아래를 모두 등록하고, `rag-agent uninstall`로 되돌립니다.

| 항목 | 내용 |
| --- | --- |
| 실행 파일 | uv 가상환경의 `pythonw.exe -m rag_agent`(콘솔 창 없이 실행) |
| 작업 스케줄러 | 하루 4회(기본 09:00, 13:00, 18:00, 22:00), "예약된 시작 시간을 놓친 경우 가능한 빨리 작업 시작", "이미 실행 중이면 새 인스턴스 시작 안 함", 사용자 로그온 상태에서만 실행 |
| URL 프로토콜 | `HKCU\Software\Classes\chanjin-sync`에 `URL Protocol` 등록, 명령은 `pythonw.exe -m rag_agent protocol "%1"`. `run`과 `reveal`을 처리(10.2) |
| 토큰 | `rag-agent login`이 토큰을 입력받아 keyring(Windows 자격 증명 관리자)에 저장 |
| 로컬 상태 | `%LOCALAPPDATA%\chanjin-rag\state.db`(SQLite), 로그는 같은 폴더의 `agent.log`(7일 순환) |

**CLI 명령:** `rag-agent sync`(수동 실행, F-03), `rag-agent status`, `rag-agent install`, `rag-agent uninstall`, `rag-agent login`, `rag-agent restore-info`(복구 안내 출력)

**구현 세부(`agent/rag_agent/`):**

- 모듈: `scan`(스캔·해시 캐시), `state`(state.db), `rules`(범위·제외 규칙), `client`(Sync API), `sync`(실행 흐름·heartbeat), `lock`(단일 인스턴스), `protocol`(`run`·`reveal`), `install`, `credentials`(토큰·서버 주소), `logs`(agent.log), `cli`.
- 명령 옵션: `sync --source cli|web|schedule`(작업 스케줄러는 `schedule`), `install --dry-run`·`uninstall --dry-run`(등록할 작업 XML과 레지스트리 키·값을 출력만), `login --server <주소>`(토큰은 숨긴 입력으로 받아 keyring 서비스 `chanjin-rag`/`agent-token`에 저장, 서버 주소는 state.db), `protocol <URL>`(URL 프로토콜 진입점).
- `state.db` 테이블: `files`(path\_key, NFC 경로, 파일 시스템 경로, 크기, 수정 시각(ns), SHA-256, reveal 키), `retry_queue`(업로드·메타 등록 실패: 이유, 시도 횟수), `kv`(서버 주소, 마지막 `sync_config`, 마지막 실행 결과), `protocol_calls`(반복 호출 제한 기록).
- 해시 캐시 키는 path\_key이고, (크기, 수정 시각)이 같으면 파일을 열지 않고 저장된 해시를 씁니다. 이번 스캔에서 보지 못한 항목은 지우되, `scan_errors` 아래 항목은 남깁니다.
- 경로: manifest에는 NFC 경로를 보내고, 파일은 파일 시스템이 준 경로 그대로 엽니다(NFD 이름 파일을 NFC 경로로 열면 찾지 못함).
- 재시도 큐: 업로드 실패(5xx, 연결 오류, `hash_mismatch` 등)를 남기고 실행은 `succeeded`로 끝냅니다(통계 `upload_failed`). 다음 실행의 manifest 판정에 다시 `upload`로 나오면 올리고, 성공하거나 더 이상 업로드 판정이 아니면 큐에서 지웁니다. 401(토큰 폐기)은 실행을 `failed`로 끝냅니다.
- 완료 통계(`stats`): `files`, `hashed`, `hash_reused`, `skipped_formats`(형식별 건수), `excluded`, `links_skipped`, `scan_errors`, `uploaded`, `upload_unchanged`, `upload_failed`, `meta_registered`, `meta_failed`, `vanished`(판정 뒤 사라진 파일), `retry_queue`.
- 단일 인스턴스: 이름 있는 뮤텍스 `Local\chanjin-rag-agent`. 이미 있으면 서버에 요청하지 않고 끝냅니다.
- 로그(`agent.log`, JSON 한 줄, 자정 순환 7개 보관): `agent.sync.started`, `agent.scan.completed`, `agent.manifest.judged`, `agent.upload.failed`, `agent.file.vanished`, `agent.heartbeat.failed`, `agent.sync.completed`, `agent.sync.failed`, `agent.instance.already_running`, `agent.protocol.{ran,revealed,rejected,rate_limited,not_found}`. 경로·파일명·본문·토큰은 남기지 않고 `run_id`, 건수, 해시 앞 8자리만 남깁니다. 토큰이 메시지에 섞여도 가리는 필터를 둡니다.
- 개발·테스트용 환경 변수: `RAG_AGENT_HOME`(상태 폴더, 이때 뮤텍스 이름에 폴더 해시를 붙여 실제 Agent와 겹치지 않음), `RAG_AGENT_SERVER`, `RAG_AGENT_TOKEN`, `RAG_AGENT_SCAN_ROOTS`(`;` 구분). `RAG_AGENT_SCAN_ROOTS`는 서버 루트를 로컬에서 좁히기만 하고(서버 루트 밖 값은 버림), 스캔하지 않은 서버 루트는 `scan_errors`로 보내 그 아래 문서가 삭제로 판정되지 않게 합니다. E2E가 실제 문서 폴더를 읽지 않고 Agent를 실행하는 데 씁니다.

**실패 알림:** MVP에서는 Agent가 실패를 `agent.log`에만 남기고, 웹의 "마지막 동기화가 하루 넘음" 경고 배지로 알아챕니다. 토큰 폐기(401)나 서버 연결 실패를 Windows 알림으로 띄우는 기능은 후속(F-21)입니다.

### 10.2 URL 프로토콜 보안

웹 페이지라면 어디서든 `chanjin-sync://` 링크를 걸 수 있으므로, Agent는 URL을 신뢰하지 않습니다.

- 허용하는 동작은 `run`과 `reveal` 두 가지뿐이고, 정해진 매개변수 외에는 모두 무시합니다. 경로, 명령, 서버 주소를 URL로 받지 않습니다.
- `run`: `source`만 받습니다. 동기화 대상과 서버 주소는 항상 로컬 설정과 서버의 `sync_config`에서 가져옵니다.
- `reveal`: `k`(path\_key의 SHA-256, 64자리 16진수)만 받습니다. 그 밖의 값은 거절합니다.
  - Agent는 로컬 `state.db`에서 이 해시로 경로를 찾습니다. 해시는 스캔할 때 미리 계산해 둡니다.
  - 그 경로가 현재 수집 루트 안에 있고, 제외 패턴에 걸리지 않고, 실제로 존재하는 파일일 때만 `explorer.exe /select,<경로>`를 인자 배열로 실행합니다. 셸을 거치지 않습니다.
  - 파일을 실행하거나 여는 동작은 없습니다. 조건에 맞지 않으면 아무것도 하지 않고 `agent.log`에만 남깁니다.
- 짧은 시간에 반복 호출되면(동작별로 1분 안에 2회 이상, `reveal`은 10초 안에 3회 이상) 무시합니다.
  - 구현: `run`은 60초 안에 이미 처리한 호출이 있으면 무시(2회째부터), `reveal`은 10초 안에 이미 2회 처리했으면 무시(3회째부터)합니다. 호출마다 새 프로세스가 뜨므로 기록은 `state.db`에 두고, 형식 검사를 통과한 호출만 셉니다.
- 형식 세부: 동작 이름과 스킴은 대소문자를 무시하고 `chanjin-sync://run/`처럼 끝 `/`는 허용합니다. 그 밖의 경로, 사용자 정보·포트가 있으면 거절합니다. `run`의 `source`는 `web`·`cli`·`schedule` 중 하나이고 없으면 `web`, 같은 매개변수가 두 번 오면 거절합니다. `k`는 대문자 16진수도 받아 소문자로 바꿉니다.
- `k` 계산: `sha256(path_key의 UTF-8 바이트)`의 16진수 소문자. `path_key`는 `documents.path_key`와 같은 식(NFC → 소문자 → NFC)입니다. Agent는 스캔 때 각 파일의 `k`를 `state.db`에 저장해 둡니다.
- `reveal`은 마지막 동기화 때 저장한 `sync_config`(state.db)로 범위를 확인하고, 실제 파일 경로가 링크로 허용 폴더 밖을 가리키는 경우도 거절합니다. 탐색기는 `%SystemRoot%\explorer.exe`를 `["explorer.exe 경로", "/select,<경로>"]` 인자 배열로 실행합니다.
- 최악의 경우에도 "평소의 동기화를 한 번 더 실행"하거나 "내 파일의 위치를 탐색기로 보여주는 것" 이상의 일이 일어나지 않게 합니다.

### 10.3 백업 수신

- 동기화가 끝나면 `/sync/backups/latest`를 조회합니다. `C:\Chanjin\dev\backup`에 없는 파일만 내려받습니다.
- 내려받은 뒤 SHA-256을 검증하고, 실패하면 파일을 지우고 다음 실행에서 다시 받습니다.
- 날짜순으로 7개를 넘는 오래된 파일은 삭제합니다.
- 이 폴더는 고정 제외(`/backup/`)에 들어 있어 웹에서 제외 패턴을 바꿔도 다시 업로드되지 않습니다.
- **구현(`agent/rag_agent/backup.py`의 `BackupFetcher`):** 동기화가 `succeeded`로 끝났을 때만 실행하고, 실패해도 동기화 결과는 바꾸지 않습니다. 저장 위치는 `RAG_AGENT_BACKUP_DIR`, 없으면 Windows는 `C:\Chanjin\dev\backup`(그 밖 OS는 개발용으로 상태 폴더 아래 `backup`). 서버 목록에서 이름 규칙(`rag-YYYYMMDD.dump.age`)·SHA-256 형식에 맞지 않는 항목은 무시하고(폴더 밖에 쓰지 않음), 같은 이름이 있으면 받지 않습니다. 받는 중에는 `<이름>.part`에 쓰고 검증이 끝나면 옮깁니다. 정리는 이름(날짜)순이며 규칙에 맞지 않는 파일은 건드리지 않습니다. `rag-agent sync` 출력에 `백업: 내려받음 N, 이미 있음 N, 검증 실패 N, 실패 N, 정리 N`을 붙입니다.
- 로그(`agent.log`, 파일명은 `file` 필드): `agent.backup.downloaded`(`file`, `size`, `sha256_prefix`), `agent.backup.hash_mismatch`, `agent.backup.failed`(`stage`: `list`·`mkdir`·`download`), `agent.backup.pruned`, `agent.backup.checked`. 서버 쪽은 `backup.listed`·`backup.downloaded`(8.2).

## 11. 보안

| 영역 | 구현 |
| --- | --- |
| 네트워크 | 공인 인바운드 없음, Tailnet 안에서 HTTPS(`tailscale serve`), Tailscale ACL로 내 기기만 허용 |
| 웹 세션 | 32바이트 난수 쿠키 `sid`, `HttpOnly`·`Secure`·`SameSite=Strict`, 14일 슬라이딩 만료, DB에는 해시만 저장 |
| CSRF | `SameSite=Strict`에 더해, GET이 아닌 요청은 `X-Requested-With` 헤더와 `Origin` 검사 |
| CSP | Nginx가 `default-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'`을 붙입니다. 답변 마크다운의 외부 이미지와 링크는 렌더링하지 않습니다(9.2) |
| 프롬프트 인젝션 | 문서 청크는 구분자로 감싸 자료로만 넣고, 그 안의 지시를 따르지 않게 시스템 프롬프트에 적습니다. 모델 출력은 위 CSP와 렌더링 규칙으로 밖으로 새지 않게 막습니다 |
| 비밀번호 | argon2id 해시, 5회 실패 시 15분 잠금 |
| Agent 토큰 | 32바이트 난수, DB에는 해시만 저장, PC에서는 keyring에 보관, 폐기 가능(`revoked_at`) |
| 업로드 | 경로는 `sync_config`의 루트 안이고 고정 제외에 걸리지 않는지 서버에서 다시 검증, 크기 상한(Nginx `client_max_body_size 12m`, `max_file_mb`를 바꿀 때 함께 바꿈), 확장자 화이트리스트(대소문자 무시), 해시 재검증 |
| 파서 격리 | 파싱은 Worker의 하위 프로세스에서 타임아웃(60초)과 메모리 상한(Linux `RLIMIT_AS`, Windows Job Object)을 두고 실행. 실패해도 Worker 본체는 유지(3.4) |
| 비밀값 | `.env`(권한 600)에만 두고 Git에는 `.env.example`만 올립니다. 대상: `NVIDIA_LLM_API_KEY`, `NVIDIA_EMBED_API_KEY`, `NVIDIA_RERANK_API_KEY`, `DATABASE_URL`, `BACKUP_AGE_RECIPIENT`(공개키) |
| 저장 데이터 | OCI 부트 볼륨 기본 암호화. 원본은 추출 직후 삭제. 백업은 age로 암호화하고 개인키는 VM에 두지 않음. 개인키는 PC와 비밀번호 관리자(또는 오프라인 매체) 두 곳에 보관 |
| 데이터 삭제 | 범위 제외는 즉시, 로컬 삭제는 30일 뒤 영구 삭제(6.5) |
| 로그 | API 키, 쿠키, 토큰은 기록하지 않습니다. 청크 본문과 질문 원문은 애플리케이션 로그에 남기지 않고 질의 로그(DB)에만 저장합니다. 앱 로그에는 질문 길이와 대화 ID만 남깁니다. 개발·E2E(`LOG_QUERY_TEXT=true`)에서만 질문 원문을 기록할 수 있습니다 |
| 외부 전송 | NVIDIA API에는 청크와 질문 텍스트만 보냅니다. 파일 경로는 프롬프트의 출처 표기용으로만 넣습니다 |

## 12. 운영

### 12.1 설정값

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `NVIDIA_LLM_API_KEY` | | 필수. LLM 모델용 키 |
| `NVIDIA_EMBED_API_KEY` | | 필수. 임베딩 모델용 키 |
| `NVIDIA_RERANK_API_KEY` | | `RERANK_ENABLED=true`면 필수. 재정렬 모델용 키 |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | |
| `LLM_MODEL` | `nvidia/nemotron-3.5-lightning-30b-a3b` | 13장 비교로 선정. 예비 후보 `z-ai/glm-5.3-flash` |
| `LLM_EXTRA_BODY` | `{"chat_template_kwargs":{"enable_thinking":false}}` | 모델별 추가 요청값(JSON). 추론 끄기(7.1) |
| `LLM_TEMPERATURE`, `LLM_MAX_TOKENS` | `0.2`, `2048` | 7.1의 LLM 설정. 호출마다 덮어쓸 수 있음 |
| `LLM_READ_TIMEOUT` | `180` | LLM 읽기 타임아웃(초). 무료 엔드포인트는 대기열이 길 수 있음(13장) |
| `PROVIDER_READ_TIMEOUT` | `60` | 임베딩·재정렬 읽기 타임아웃(초) |
| `EMBED_MODEL` | `nvidia/nemotron-3.5-embed-8b` | |
| `RERANK_MODEL`, `RERANK_URL` | Nemotron 3.5 Rerank 8B, `https://ai.api.nvidia.com/v1/retrieval/nvidia/nemotron-3.5-rerank-8b/reranking` | 엔드포인트는 모델 페이지 기준(`scripts/check_nvidia.py`로 확인) |
| `RERANK_ENABLED` | `true` | 평가 결과로 결정 |
| `EVAL_JUDGE_MODEL` | `meta/llama-3.3-70b-instruct` | 평가 판정 모델. 답변 모델과 같으면 `rag-eval run`이 거절 |
| `NVIDIA_JUDGE_API_KEY` | | `rag-eval run`(PROVIDER=nvidia)에서 필수. 판정 모델용 키 |
| `EMBED_DAILY_FILE_LIMIT` | `100` | |
| `EMBED_DAILY_CHUNK_LIMIT` | `0`(제한 없음) | 13장 3번 실측 후 확정(6.2) |
| `SYNC_ALLOWED_ROOT` | `C:\Chanjin\dev` | 수집 허용 최상위 폴더(PRD 3장). 고정 제외 `/project/`, `/backup/`의 기준. 테스트·E2E는 임시 폴더 |
| `UPLOAD_TMP_DIR` | (비움: 저장소 `var/tmp`) | 업로드 원본 임시 보관(3.2 `tmp` 볼륨). 추출 후 삭제 |
| `JOB_MAX_ATTEMPTS` | `5` | 작업 재시도 상한(6.2) |
| `JOB_STALE_LOCK_MINUTES` | `10` | `locked_at`이 이만큼 지난 `running` 작업은 다시 대기열로 |
| `JOB_RATE_LIMIT_DELAY_MINUTES` | `30` | 429가 계속될 때 작업을 미루는 시간 |
| `WORKER_POLL_SECONDS` | `5` | 빈 큐 확인 간격 |
| `PARSER_TIMEOUT_SECONDS` | `60` | 파서 하위 프로세스 시간 상한(초, 3.4) |
| `PARSER_MEMORY_LIMIT_MB` | `1024` | 파서 하위 프로세스 메모리 상한(MB, 0이면 없음, 3.4) |
| `EMBED_BATCH_SIZE` | `1` | 무료 엔드포인트는 묶음 요청에 500(13장 실측). 유료·자체 호스팅이면 32 |
| `EMBED_DIM` | `2048` | 모델 기본 4096차원을 `dimensions`로 줄임(6.3) |
| `KIWI_USER_DICT_PATH` | (비움: `deploy/kiwi_user_dict.txt`) | Kiwi 사용자 사전 경로(6.4). 파일이 없으면 사전 없이 동작 |
| `LOCAL_DELETED_RETENTION_DAYS` | `30` | 로컬 삭제 문서 보관 기간(6.5) |
| `MASS_DELETE_GUARD_RATIO` | `0.2` | 한 번에 이 비율을 넘는 삭제 판정은 보류(6.1) |
| `SYNC_RUN_STALE_MINUTES` | `10` | 하트비트가 끊긴 동기화 실행을 `abandoned`로 바꾸는 시간 |
| `WORKER_STALE_SECONDS` | `90` | `/health`가 Worker 하트비트 끊김으로 보는 시간(초) |
| `DISK_CHECK_PATH` | (비움 = 업로드 임시 폴더) | `/health`가 여유 공간을 확인할 경로 |
| `DISK_MIN_FREE_GB` | `10` | 디스크 여유가 이보다 작으면 `/health` 경고 |
| `LOG_QUERY_TEXT` | `false` | 앱 로그에 질문 원문 기록 여부(개발·E2E 전용) |
| `CHAT_TIMEOUT_SECONDS` | `60` | 질의 하나의 전체 시간 상한(7.3 `timeout`) |
| `FAKE_LLM_TOKEN_DELAY_MS` | `0` | `PROVIDER=fake` 전용. Fake LLM 조각 사이 지연. E2E는 150으로 두어 단계 표시를 화면에서 확인 |
| `APP_ENV` | `dev` | `dev` \| `prod` \| `test`. 프로세스 환경 변수로 `test`를 주면 `.env`를 읽지 않고 NVIDIA 키를 비웁니다(pytest·E2E) |
| `PROVIDER` | `nvidia` | `nvidia` \| `fake`. `fake`는 결정적인 가짜 구현(14장). `APP_ENV=test`에서는 `fake`만 허용 |
| `DATABASE_URL` | `postgresql+psycopg://rag:rag@localhost:5432/rag` | 테스트·E2E는 `TEST_DATABASE_URL`(테스트 DB, 5433)을 씁니다 |
| `TZ` | `Asia/Seoul` | 한도 초기화와 예약 작업 기준 |
| `BACKUP_AGE_RECIPIENT` | | age 공개키 |
| `BACKUP_KEEP_ON_VM` | `3` | |
| `BACKUP_DIR` | (비움: 저장소 `var/backups`) | VM 백업 폴더(`backups/`). Worker와 API가 같은 폴더를 씀 |
| `BACKUP_PG_DUMP_CMD`, `BACKUP_AGE_CMD` | `["pg_dump"]`, `["age"]` | 백업 외부 명령(JSON 목록, 실행 파일과 앞쪽 인자). 인자는 코드가 붙임. E2E는 가짜 명령(`web/e2e/fixtures`) |

### 12.2 백업과 복구

- **매일 03:00(한국 시간):** Worker의 APScheduler가 `pg_dump -Fc` 결과를 `age -r <공개키>`로 암호화합니다. 파일명은 `backups/rag-YYYYMMDD.dump.age`이고, VM에는 최근 3개만 둡니다. 이를 위해 `backend` 이미지에 PostgreSQL 17 클라이언트를 넣습니다.
- **구현(`backend/app/lifecycle/backup.py`의 `create_backup`):** `pg_dump -Fc -f <임시 평문> --dbname=<비밀번호 뺀 주소>`(비밀번호는 `PGPASSWORD`) → `age -r <공개키> -o <임시 암호문> <임시 평문>` → `rag-YYYYMMDD.dump.age`로 옮기고 SHA-256을 `<이름>.sha256`에 씀 → 이름(날짜)순으로 최근 `BACKUP_KEEP_ON_VM`개만 남김(옆 파일 포함). 임시 평문·암호문은 성공·실패와 관계없이 지웁니다. 같은 날 다시 만들면 그날 파일을 덮습니다. 외부 명령은 주입 가능한 실행기(`CommandRunner`, 기본 `subprocess` 인자 목록 실행, 셸 없음)로 감싸 pytest는 가짜 실행기로 확인합니다. Worker 예약 작업 ID `backup`(매일 03:00), 수동 실행은 `rag-backend backup` 또는 `deploy/scripts/backup.sh`(직접 `pg_dump | age`, `--compose`면 worker 컨테이너에서 `rag-backend backup`). 로그: `backup.created`(`name`, `size`, `sha256_prefix`, `kept`, `removed`), `backup.failed`(`stage`: `config`·`pg_dump`·`age`·`store`, `error`: `no_recipient`·`exit_<코드>` 등). DB 주소·비밀번호·공개키는 남기지 않습니다. 동기화 화면 "최근 백업"은 가장 새 백업 파일의 시각과 이름입니다.
- **PC 보관:** Sync Agent가 실행될 때마다 내려받아 7개를 보관합니다(10.3).
- **복구 절차(`deploy/scripts/restore.sh <백업> [개인키]`, 옆에 `.sha256`이 있으면 먼저 검증, 덮어쓰기 확인 후 진행):**
  1. 새 VM에 Compose를 띄웁니다. 이때 `db`만 띄웁니다.
  2. PC에서 백업 파일을 VM으로 복사합니다(Tailscale `scp`).
  3. `age -d -i 개인키`로 복호화합니다. 개인키는 PC에서만 쓰므로 복호화는 PC에서 하고 평문 덤프를 옮기는 방법도 허용합니다.
  4. `pg_restore`로 복원한 뒤 `api`와 `worker`를 띄웁니다.
  5. 다음 Agent 실행에서 manifest 비교로 백업 이후 바뀐 파일만 다시 올라옵니다.
- **복구 훈련:** 구축 단계에서 한 번 실제로 복구해 절차를 확인합니다. 이때 PC가 아닌 곳에 보관한 개인키 사본으로 복호화되는지도 확인합니다.

### 12.3 배포

- VM에서 `git pull` → `docker compose build` → `docker compose up -d` 순서로 배포합니다. arm64 이미지를 VM에서 직접 빌드하므로 크로스 빌드와 레지스트리가 필요 없습니다.
- DB 스키마는 `migrate` 서비스가 `alembic upgrade head`로 올립니다. 되돌리기 어려운 마이그레이션 전에는 수동 백업을 먼저 받습니다.
- 개발은 Windows PC의 Docker Desktop에서 같은 Compose 파일로 `db`만 띄우고, `api`와 `worker`는 uv로 직접 실행합니다.

### 12.4 모니터링

- `/health`(공개 요약)와 `/health/details`(세션)가 DB 연결, Worker 하트비트(30초마다 DB에 기록, 90초 넘으면 이상), 디스크 여유(10GB 미만이면 경고), NVIDIA API 마지막 성공·실패 시각과 오류 코드를 반환합니다. API 키 오류(401·403)나 크레딧 소진(402)은 웹 전체 배너로 보여줍니다.
- 동기화 화면에 마지막 성공 동기화 시각과 마지막 백업 시각을 표시합니다. 하루 넘게 갱신되지 않으면 경고 배지를 띄웁니다.
- 별도 모니터링 시스템은 두지 않습니다.

### 12.5 리소스 예산

| 항목 | 예상 |
| --- | --- |
| 디스크(47GB) | OS 약 10GB, Docker 이미지 약 5GB, DB 수 GB(청크 수만 개 × 벡터 + HNSW + 추출 텍스트), VM 백업 3개, 나머지는 여유 |
| PostgreSQL | `shared_buffers=1GB`, `maintenance_work_mem=1GB`(HNSW 생성), `work_mem=32MB` |
| Worker 동시성 | 파싱은 1개씩(CPU 2개), 임베딩 API 호출은 동시 2개 |
| API | Uvicorn 프로세스 1개(async), SSE 연결은 사용자 1명 기준 |

## 13. 구축 초기 검증

본격 개발 전에 아래를 먼저 확인합니다. 결과에 따라 이 문서의 해당 절을 고칩니다. 2·3번은 `uv run python scripts/check_nvidia.py`로 확인합니다.

**실측 결과 (2026-11-07 23시, 무료 엔드포인트, 모델별 키)**

| 항목 | 결과 | 반영 |
| --- | --- | --- |
| 모델 ID | `deepseek-ai/deepseek-v4.1-flash`, `nvidia/nemotron-3.5-embed-8b` 모두 모델 목록에 있음 | 12.1 |
| 임베딩 차원 | 기본 4096차원, `dimensions=2048`이면 2048차원으로 줄어듦 | 6.3 ①(2048, `halfvec`) 확정 |
| 임베딩 묶음 | 문장 1개씩은 12/12 성공, 3개 묶음은 12/12 실패(HTTP 500 `service inference failed`) | 6.2, `EMBED_BATCH_SIZE=1` |
| 임베딩 지연 | 질문 1개 약 0.2~0.5초 | 7.4 예산(0.5초) 안 |
| 재정렬 | `ai.api.nvidia.com/.../reranking` 정상, 관련 문장 1위, 약 0.2~1.6초 | 7.4 예산(0.8초) 근처 |
| LLM 응답(deepseek) | 한국어 답변 정상. 추론을 끄면(`thinking=false`) 추론 텍스트 없음 | 7.1 답변 후처리 |
| LLM 지연(deepseek) | 응답 시작까지 대기 66~81초, 첫 답변 글자 79~94초, 전체 86~99초(3회). PRD 7장 목표(첫 토큰 3초, p95 8초)와 큰 차이 | 아래 비교 후 모델 변경 |
| 인증 오류 | 틀린 키에 401이 아니라 403 `Authorization failed` | T03 오류 변환 |
| 키 범위 | LLM 키로 카탈로그의 다른 LLM도 호출됨 | 모델을 바꿔도 키 재발급 불필요 |

**LLM 비교 (2026-11-07, 같은 RAG 프롬프트: 자료 3개 + 인용 번호 + 한국어, 질문 3개 중 1개는 답 없음)**

| 모델 | 첫 답변 글자 | 결과 |
| --- | --- | --- |
| **`nvidia/nemotron-3.5-lightning-30b-a3b`(`enable_thinking=false`)** | **0.4~0.5초** | 인용 정확, 답 없는 질문에 "찾지 못했습니다", 추론 텍스트 없음, 답이 짧음 → **선정** |
| `z-ai/glm-5.3-flash` | 1.1~9.1초(중앙값 2.0) | 인용 정확, 추론 끄기 옵션이 적용되지 않음, 답이 조금 더 자세함 → 예비 후보 |
| `mistralai/mistral-large-4-1t-a52b` | 15초 | 추론 때문에 느림 |
| `deepseek-ai/deepseek-v4.1-flash`, `minimaxai/minimax-m3.1-flash` | 60~70초 | 무료 서버 대기열 |
| `google/gemma-4-31b-it`, `openai/gpt-oss-20b` | 180초 초과 | 시간 초과 |
| `google/gemma-3-12b-it`, `nvidia/nemotron-3-super-120b-a12b`, `moonshotai/kimi-k2.6` | - | 404(목록에만 있고 호출 불가) |

긴 문서에서의 답변 품질은 평가셋(PRD 8장)으로 다시 확인하고, 부족하면 예비 후보와 비교합니다.

| 순서 | 확인 항목 | 통과 기준 | 실패 시 |
| --- | --- | --- | --- |
| 1 | ParadeDB 이미지의 arm64 동작, pg\_search와 pgvector 동시 사용, 공백 토크나이저 | Oracle Linux 9 VM에서 BM25 + 벡터 RRF 쿼리 실행 성공 | 공식 PostgreSQL + pgvector 이미지로 바꾸고 BM25는 애플리케이션 내 색인(bm25s) |
| 2 | 임베딩 실제 차원, 한 요청당 최대 입력 수, `modality` 값 | 차원 확인 후 6.3 표대로 컬럼 타입 확정 | 6.3 ②, 또는 색인 없는 전수 비교 |
| 3 | NVIDIA API 지연, 429 동작과 응답 헤더, 재정렬 엔드포인트, LLM의 추론 텍스트 출력 여부 | 7.4 예산 안 | 7.4의 줄이는 순서 적용 |
| 4 | 내 HWP/HWPX 파일 10개 이상 파싱 품질 | 본문과 표 텍스트 누락 없음 | 다른 HWP 파서 비교, 실패 사유 기록 후 후속 과제로 |
| 5 | OCI 프리 티어 유휴 인스턴스 회수 조건 | 이 플랫폼의 평소 부하로 회수 조건에 걸리지 않음 | 계정 유형 변경 검토, 백업 주기 확인 |
| 6 | `tailscale serve` HTTPS, Chrome·Edge의 `chanjin-sync://` 실행(`run`, `reveal`) | 웹 버튼으로 Agent 실행, "폴더에서 보기"로 탐색기 선택 표시, Secure 쿠키 동작 | 웹 버튼은 안내만 표시하고 CLI 사용, "폴더에서 보기"는 경로 복사로 대체 |
| 7 | 백업 → 새 DB 복구 | 복구 후 검색 결과 동일 | 절차 수정 |

## 14. 테스트 전략

| 수준 | 대상 | 방법 |
| --- | --- | --- |
| 단위 | 형식별 파서(암호 걸린 파일, 손상 파일, 빈 문서 포함), 청커, Kiwi 토큰화, RRF 합산, 인용 후처리, manifest 판정(이동, 복사, 읽지 못한 폴더, 대량 삭제 보호), 제외 패턴(고정 제외가 하위 같은 이름 폴더를 빼지 않는지), 요약 대상 판정 | pytest, `backend/tests/fixtures`에 형식별 샘플 파일(HWP 포함) |
| 실제 문서 | 실제 문서 폴더에서 파서·청커가 깨지지 않는지(개수와 비율만 확인) | pytest `samples` 마커. 기본 실행에서 빼고, `SAMPLE_DOCS_DIRS`(폴더 여러 개, Windows는 `;`로 구분)가 있으면 `verify.sh`가 `pytest -m samples`를 따로 실행. 내용·파일명은 저장소와 로그에 남기지 않음 |
| 통합 | Hybrid SQL, 작업 큐와 일일 한도(버전당 1회 차감, 청크 임베딩 재사용, 청크 상한), 처리 중 새 버전 도착 시 결과 폐기, 청크 교체 트랜잭션, 영구 삭제와 인용 가림 | Docker의 ParadeDB 테스트 DB(`deploy/compose.test.yaml`, 포트 5433, tmpfs)에 pytest 실행. `scripts/verify.sh`가 띄우고 `TEST_DATABASE_URL`을 넘김. 테스트마다 트랜잭션 롤백 |
| API | 인증, Sync API, SSE 이벤트 순서(일반, 찾지 못함, 요약, 오류, 409) | httpx `AsyncClient`, Provider는 Fake 구현(고정 벡터·고정 답변) |
| E2E | 화면 조작·화면 결과·백엔드 로그(`waitForBackendLog`) | Playwright + 설치된 Chrome(`web/e2e`). 모델 오류는 질문에 `[[fake-error-once:<code>]]`(`rate_limited`·`provider_unavailable`·`provider_auth`) 표식을 넣어 재현합니다. Fake LLM이 그 질문의 첫 모델 호출에서만 해당 예외를 내므로 "다시 시도"가 성공합니다(`PROVIDER=fake` 전용) |
| Agent | 스캔, 해시 캐시, 재시도 큐, 잠긴 파일, 프로토콜 인자 검증(`run`, `reveal`의 해시 형식과 루트 밖 경로 거절) | 임시 폴더와 가짜 서버 |
| 웹 | SSE 파서, 인용 칩, 이미지·외부 링크 차단, 동기화 버튼 흐름(실행 중, 60초 비활성화), 범위 변경 확인 창 | Vitest + MSW |
| 품질 회귀 | 검색·답변 지표 | `rag-eval run`(평가셋 30\~50문항, PRD 8장 지표). 청킹, 프롬프트, 모델, 재정렬 설정을 바꿀 때마다 실행해 이전 실행과 비교 |

**평가 CLI(`rag-eval`) 규칙**

- **Recall@5, MRR:** 정답 근거를 `(경로, 위치)` 겹침으로 판정합니다.
- **근거 충실성, 답변 정확도:** LLM-as-judge로 판정합니다. 판정 모델은 답변 모델(nemotron)과 다른 계열의 NVIDIA 카탈로그 모델로 정하고, 판정 모델 이름을 `eval_runs.config`에 기록합니다.
- **결과 저장:** `eval_runs`와 `eval_results`에 저장하고, `/eval` 화면과 CLI에서 이전 실행과 비교합니다.
- **임베딩 한도:** 평가 실행 중의 질문 임베딩은 한도에 세지 않습니다(질문 임베딩은 원래 한도 대상이 아님. `config.embed_quota = "not_counted"`).
- **평가셋 형식(`eval/datasets/*.yaml`):** `version`, `items[]`(`id`, `type`: `single`·`multi`·`table`·`not_found`·`hwp`, `question`, `answer`, `evidence[]`: `path`, `location`). `not_found`는 정답·근거 없이, 나머지는 둘 다 필수. 위치 키는 청크 위치와 같음(`page`, `slide`, `paragraph`, `table`, `row`, `sheet`, `lines`). 예제 `default.yaml`은 `eval/datasets/synthetic/`의 합성 문서 기준. `dataset_version`은 `이름@버전+내용 해시 8자리`.
- **겹침 규칙:** 경로는 대소문자·`/`·`\`를 무시하고 같아야 합니다. 라벨 위치가 `{}`면 파일 어디든 인정하고, 아니면 둘 다 가진 위치 키가 하나 이상이며 그 범위(`키`~`키_end`, `lines`는 [처음, 끝])가 모두 겹쳐야 합니다. 순위는 마지막 검색 시도의 최종 순서(재정렬 점수 내림차순, 없으면 RRF 순위)입니다.
- **지표 계산:** Recall@5는 문항별 "상위 5개와 겹친 근거 수 ÷ 근거 수"의 평균, MRR은 첫 겹침 순위의 역수 평균(답 없는 문항 제외). 정확도는 정답·부분·오답 = 1·0.5·0의 평균(답 있는 문항), 충실성은 "찾지 못함"이 아닌 답변의 인용 청크 대비 뒷받침 비율 평균, "찾지 못함" 정확도는 `not_found` 문항 중 찾지 못함으로 답한 비율, p95는 이번 실행 질의 로그(`source='eval'`)의 `latency_ms.total`(nearest-rank). 오류가 난 문항은 지표에서 빠지고 `metrics.errors`로 셉니다.
- **명령:** `rag-eval run [--dataset 경로]`(기본 `eval/datasets/default.yaml`, 끝나면 바로 이전 실행과 비교 출력), `rag-eval compare <a> <b>`(a 대비 b의 증감). 로그 `eval.run.started`, `eval.item.excluded`(경고), `eval.item.failed`, `eval.run.completed`(지표). 테스트·E2E의 판정은 Fake 판정기(`fake-judge`, 낱말 겹침).
