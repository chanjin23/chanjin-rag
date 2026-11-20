# 코드 지도

기능 흐름 순서대로 "어느 파일의 어느 함수"를 보면 되는지 정리한 문서입니다. 설계 이유와 규칙은 [TRD.md](./TRD.md)에 있고, 이 문서는 길찾기용입니다.

- 줄 번호는 금방 바뀌므로 적지 않습니다. 함수 이름으로 검색하세요.
- 백엔드 경로는 `backend/app/` 기준입니다(예: `jobs/runner.py` = `backend/app/jobs/runner.py`).
- 파일을 추가·이동·이름 변경하면 이 문서도 같이 고칩니다.

## 0. 한눈에 보기

```
[PC] agent ──HTTP(Bearer)──▶ api/sync.py ──▶ jobs 테이블 ──▶ worker(jobs/runner.py)
                                                              extract → index → embed
                                                                 │        │       │
                                                             sections   chunks  chunks.embedding
[브라우저] web ──HTTP(세션)──▶ api/chat.py ──▶ query/graph.py ──▶ query/search.py (Hybrid SQL)
```

| 실행 단위 | 시작점 | 명령 |
| --- | --- | --- |
| API 서버 | `main.py` `create_app()` → `api/router.py` | `uv run uvicorn app.main:app` |
| Worker | `cli.py` `worker()` → `jobs/worker.py` `Worker.run()` | `rag-backend worker` |
| Sync Agent | `agent/rag_agent/cli.py` `sync()` | `rag-agent sync` |
| 평가 | `eval/rag_eval/cli.py` `run()` | `rag-eval run` |
| 웹 | `web/src/main.tsx` → `web/src/routes/router.tsx` | `corepack pnpm dev` |

## 1. 수집: PC → 서버

### 1.1 Agent (`agent/rag_agent/`)

| 순서 | 파일 · 함수 | 하는 일 |
| --- | --- | --- |
| 1 | `cli.py` `sync()` → `_run_sync()` | 명령 진입점. 상태 DB와 로그 준비 |
| 2 | `sync.py` `SyncRunner.run()` | 뮤텍스(`lock.py`)로 단일 실행 보장 |
| 3 | `sync.py` `_run()` → `client.py` `get_config()` | **첫 서버 호출** `GET /api/sync/config` |
| 4 | `rules.py` `SyncRules.from_config()` | 루트·제외 패턴·확장자 규칙 생성 |
| 5 | `client.py` `start_run()` | `POST /api/sync/runs`, 이후 `Heartbeat`가 1분마다 갱신 |
| 6 | `sync.py` `_sync()` → `scan.py` `Scanner` | 폴더 순회, 바뀐 파일만 해시(`sha256_file`). 캐시는 `state.py` `StateDB` |
| 7 | `client.py` `manifest()` | 전체 목록 전송, 서버 판정 받음 |
| 8 | `sync.py` `_upload()` / `_register_meta()` | `PUT /files`(업로드), `POST /files/meta`(10MB 초과) |
| 9 | `client.py` `complete()` | `POST /runs/{id}/complete` |
| 10 | `backup.py` `BackupFetcher.fetch()` | 새 백업만 내려받아 PC에 7개 보관 |

기타: `protocol.py`(`chanjin-sync://` run·reveal 처리), `install.py`(작업 스케줄러·URL 프로토콜 등록), `credentials.py`(keyring 토큰), `keys.py`(경로 NFC·`path_key`), `logs.py`(JSON 로그·비밀값 가리기).

### 1.2 서버 접수 (`api/sync.py`, `sync/`, `ingest/intake.py`)

| 경로 | 함수 | 실제 처리 |
| --- | --- | --- |
| `GET /api/sync/config` | `api/sync.py` `get_config()` | `sync/scope.py` `load_scope()` |
| `POST /api/sync/runs` | `start_run()` | `sync/runs.py` `start_run()` |
| `POST /api/sync/runs/{id}/heartbeat` | | `sync/runs.py` `heartbeat()` |
| `POST /api/sync/runs/{id}/manifest` | | `sync/manifest.py` `judge_manifest()` → 이동·복사는 `sync/documents.py` `relocate()`·`copy_document()` |
| `PUT /api/sync/files` | | `sync/upload.py` `receive()`(해시 검증) → `ingest/intake.py` `accept_upload()` → **`extract` 작업 등록** |
| `POST /api/sync/files/meta` | | `ingest/intake.py` `accept_meta_only()`(파일명 청크만) |
| `POST /api/sync/runs/{id}/complete` | | `sync/runs.py` `complete_run()` |
| `GET /api/sync/backups/*` | | `lifecycle/backup.py` `latest_backup()`·`resolve_backup()` |

인증은 라우터 전체에 걸린 `api/deps.py` `require_sync_token` → `core/auth.py` `resolve_api_token()`.

Agent 없이 시작하기(개발·E2E): `cli.py` `ingest-local` → `ingest/local.py` `ingest_local()` → 같은 `accept_upload()`.

## 2. 처리: 파싱 → 청킹·BM25 → 임베딩

전체 순서는 **`jobs/runner.py`의 `JobRunner`** 하나에서 볼 수 있습니다. 각 단계가 끝나면 다음 단계 작업을 큐에 넣습니다.

```
jobs/worker.py  Worker._prep_loop()  ─▶ JobRunner.run() ─▶ _extract() ─enqueue─▶ _index() ─enqueue─▶
                Worker._embed_loop() ─▶ JobRunner.claim_embed() ─▶ _embed()
```

| 단계 | 함수 | 사용하는 모듈 | 결과 |
| --- | --- | --- | --- |
| 큐 | `jobs/queue.py` `enqueue()`, `lock_next()`(`SKIP LOCKED`), `requeue_stale()` | | `jobs` 테이블 |
| 파싱 | `JobRunner._extract()` | `ingest/parsers/isolation.py` `parse_in_subprocess()` → `_child.py` → `registry.py` `parse_document()` → `pdf_parser.py`·`docx_parser.py`·`pptx_parser.py`·`text_parser.py` | `sections` 저장, 임시 원본 삭제 |
| 청킹·BM25 | `JobRunner._index()` | `ingest/chunking/chunker.py` `build_chunks()`, `ingest/tokenize.py` `bm25_text()` | `chunks` 교체. 이 시점부터 키워드 검색 가능 |
| 임베딩 | `JobRunner.claim_embed()` → `_embed()` → `_embed_texts()` | `jobs/quota.py`(하루 한도), `providers/nvidia.py` `NvidiaEmbeddingProvider.embed()` | `chunks.embedding`(halfvec) 저장, 문서 `ready` |
| 오류·재시도 | `JobRunner._handle_error()` | `providers/errors.py`(Provider 예외) | `run_after` 미룸 또는 `failed` |

파서 공통 형식은 `ingest/parsers/base.py`(`Section`, `ParseResult`), 메모리 상한은 `memlimit.py`, 암호 파일 판별은 `office.py`(DOCX·PPTX 공용).

재처리: `api/documents.py` `reprocess_document()` → `index` 작업 등록.

## 3. 질의: 질문 → 답변 (SSE)

| 순서 | 파일 · 함수 | 하는 일 |
| --- | --- | --- |
| 1 | `web/src/features/chat/ChatPage.tsx` → `useChatStream.ts` `useChatStream()` | 입력, 메시지 상태 관리 |
| 2 | `web/src/api/chat.ts` `sendMessage()` → `sse.ts` `readSse()` | `POST /api/conversations/{id}/messages`, SSE 수신 |
| 3 | `api/chat.py` `post_message()` | 대화·메시지 저장, `EventSourceResponse` 반환 |
| 4 | `api/chat.py` `ChatRun.produce()` | 그래프를 별도 작업으로 돌려 이벤트를 큐로 전달 |
| 5 | `query/graph.py` `run_query()` → `build_graph()` | LangGraph 실행 |
| 6 | `query/graph.py` 노드들 | `scope_node` → `rewrite_node` → `retrieve_node` → `rerank_node` → `grade_node` → (`retry_rewrite_node`) → `generate_node` / `not_found_node` / `summarize_node` → `log_node` |
| 7 | `query/search.py` `search()` → `hybrid_search()` | 질문 임베딩 + 벡터·BM25 RRF SQL. 범위는 `compute_coverage()` |
| 8 | `query/prompts.py` `*_messages()` | 단계별 프롬프트 |
| 9 | `query/postprocess.py` `clean_answer()` | 없는 인용 번호 제거, 추론 텍스트 제거 |
| 10 | `query/graph.py` `save_query_log()` | `query_logs` 저장 |

요약 대상 고르기는 `query/summary_target.py` `resolve_summary_target()`. SSE 이벤트 이름·순서(`status → sources → token → done / error`)를 바꿀 때는 `api/chat.py`와 `web/src/api/chat.ts`를 함께 고칩니다.

## 4. 모델 호출 (`providers/`)

| 파일 | 내용 |
| --- | --- |
| `base.py` | `LLMProvider`, `EmbeddingProvider`, `RerankProvider` 인터페이스 |
| `factory.py` | `create_llm_provider()` 등. 설정에 따라 NVIDIA 또는 Fake 선택 |
| `nvidia.py` | NVIDIA 구현(openai SDK, 재정렬은 httpx), tenacity 재시도 |
| `fake.py` | 테스트·E2E용 결정적 구현. 질의 응답 규칙은 `query/fake_llm.py` |
| `monitor.py` | 호출 성공·실패를 `provider_status`에 기록(`/health`가 읽음) |
| `errors.py` | `RateLimited`, `ProviderUnavailable`, `ProviderAuthError` 등 |

## 5. 그 밖의 기능

| 기능 | 백엔드 | 웹 |
| --- | --- | --- |
| 로그인·세션 | `api/auth.py`, `core/auth.py`, `core/security.py` | `features/auth/` |
| 대화 목록·이름·삭제 | `api/conversations.py` | `features/chat/ConversationList.tsx` |
| 피드백 | `api/feedback.py` | `features/chat/FeedbackBar.tsx` |
| 문서 목록·상세·재처리·삭제 | `api/documents.py`, `ingest/purge.py` `purge_documents()` | `features/documents/` |
| 동기화 상태·지금 동기화·보류 삭제 적용 | `api/sync_web.py` | `features/sync/SyncPage.tsx` |
| 수집 범위 설정(루트·제외 패턴) | `api/settings.py`, `sync/settings.py` | `features/sync/SyncPage.tsx` |
| 검색 범위 통계 | `api/index.py` | |
| 첫 실행 체크리스트 | `api/setup.py` | `features/setup/SetupChecklist.tsx` |
| 상태 점검·전역 배너 | `api/health.py` `collect_health()` | `components/GlobalBanner.tsx`, `lib/banner.ts` |
| 평가 결과 보기 | `api/eval.py` | `features/eval/EvalPage.tsx` |
| 평가 실행 | `eval/rag_eval/cli.py` → `runner.py` `run_eval()` → `metrics.py`, `judge.py`. 평가셋은 `eval/datasets/` | |

## 6. Worker 예약 작업 (`jobs/worker.py` `build_scheduler()`)

| 작업 ID | 주기 | 함수 |
| --- | --- | --- |
| 하트비트 | 30초 | `_heartbeat_job()` → `worker_heartbeats` |
| 멈춘 작업 재대기 | 1분 | `_stale_job()` → `jobs/queue.py` `requeue_stale()` |
| 끊긴 동기화 정리 | 1분 | `_sync_runs_job()` → `sync/runs.py` `abandon_stale_runs()` |
| `quota_midnight` | 매일 0시 | `_midnight_job()` 임베딩 루프 깨우기 |
| `backup` | 매일 3시 | `_backup_job()` → `lifecycle/backup.py` `create_backup()` |
| `retention_purge` | 매일 4시 | `_retention_job()` → `lifecycle/retention.py` `purge_expired()` |

## 7. 공통 기반

| 파일 | 내용 |
| --- | --- |
| `core/config.py` | `Settings`(환경 변수·`.env`) |
| `core/logging.py` | structlog 설정(`LOG_FILE`, `LOG_FORMAT`) |
| `db/models.py` | 모든 테이블 모델. 마이그레이션은 `backend/alembic/versions/` |
| `db/engine.py` | 엔진·세션 생성 |
| `db/enums.py` | 상태·종류 값(문자열 + CHECK) |
| `api/router.py` | 라우터 묶음. 공개 / 웹(세션+CSRF) / Sync(Bearer) 구분 |
| `api/deps.py` | 세션·CSRF·토큰 의존성 |
| `api/errors.py` | 오류 응답 형식 |
| `cli.py` | `rag-backend` 명령: `migrate`, `create-user`, `create-token`, `ingest-local`, `worker`, `backup`, `purge-retention`, `search` 등 |
| `web/src/api/client.ts` | `apiFetch()` 공통 요청(CSRF, 401 처리) |

## 8. 테스트와 배포

| 대상 | 위치 |
| --- | --- |
| 백엔드 단위·통합 테스트 | `backend/tests/` (파일 이름이 기능 이름과 대응: `test_sync_manifest.py`, `test_graph.py` …) |
| Agent 테스트 | `agent/tests/` |
| 평가 테스트 | `eval/tests/` |
| E2E | `web/e2e/*.spec.ts`, 백엔드 로그 확인은 `web/e2e/backendLog.ts` `waitForBackendLog` |
| 전체 검사 | `scripts/verify.sh` |
| 배포 | `deploy/compose.yaml`, `deploy/nginx.conf`, `deploy/scripts/` |
| 개발용 DB | 루트 `docker-compose.yml`, 테스트 DB는 `deploy/compose.test.yaml` |
