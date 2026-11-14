import path from "node:path";
import { defineConfig, type ReporterDescription } from "@playwright/test";
import {
  API_PORT,
  API_URL,
  BACKEND_LOG,
  BACKUP_DIR,
  STORAGE_STATE,
  SYNC_ROOT,
  TEST_DATABASE_URL,
  UPLOAD_TMP_DIR,
  WEB_PORT,
  WEB_URL,
} from "./e2e/env";

// 루프 실행기가 LOOP_REPORT_DIR을 주면 JSON 리포트도 남긴다(필수 E2E를 제목으로 확인하는 데 씀).
const reporters: ReporterDescription[] = [
  ["list"],
  ["html", { outputFolder: "./e2e/.report", open: "never" }],
];
if (process.env.LOOP_REPORT_DIR) {
  reporters.push(["json", { outputFile: path.join(process.env.LOOP_REPORT_DIR, "e2e.json") }]);
}

/**
 * E2E 백엔드 환경 변수.
 * - APP_ENV=test: 저장소 .env(실제 API 키)를 읽지 않는다. PROVIDER=fake: 모델 호출은 Fake Provider.
 * - LOG_QUERY_TEXT=true: 질문 원문을 로그에 남겨, 테스트가 이번 요청의 로그 줄을 찾을 수 있게 한다.
 * - LOG_FORMAT=json + LOG_FILE: 테스트가 읽는 JSON 로그 파일(e2e/backendLog.ts).
 * - SESSION_COOKIE_SECURE=false: E2E는 로컬 HTTP라 Secure 쿠키를 끈다(운영은 항상 Secure).
 * - UPLOAD_TMP_DIR: Sync 업로드 원본 임시 폴더. `ingest-local`(Worker 처리)과 같은 폴더를 써서
 *   E2E가 업로드한 파일을 CLI로 처리할 수 있게 한다.
 * - BACKUP_DIR: VM 백업 폴더. lifecycle.spec이 `rag-backend backup`(가짜 pg_dump·age)으로 만든 백업을
 *   `/sync/backups*`로 내려주고, 동기화 화면의 "최근 백업"에 보인다.
 * - SYNC_ALLOWED_ROOT: Windows는 운영과 같은 `C:\Chanjin\dev`, Linux(WSL)는 E2E 임시 폴더(env.ts
 *   `SYNC_ROOT`). Sync API는 경로를 문자열로만 검사하고, Agent E2E는 이 아래에 실제 파일을 만든다.
 * - FAKE_LLM_TOKEN_DELAY_MS: Fake LLM 조각 사이 지연. 질의 단계 표시("답변 작성 중" 등)가 화면에
 *   잠깐이라도 머물러 테스트가 확인할 수 있게 한다.
 */
const BACKEND_ENV = {
  APP_ENV: "test",
  PROVIDER: "fake",
  LOG_QUERY_TEXT: "true",
  LOG_FORMAT: "json",
  LOG_FILE: BACKEND_LOG,
  DATABASE_URL: TEST_DATABASE_URL,
  SESSION_COOKIE_SECURE: "false",
  UPLOAD_TMP_DIR,
  BACKUP_DIR,
  SYNC_ALLOWED_ROOT: SYNC_ROOT,
  FAKE_LLM_TOKEN_DELAY_MS: "150",
  PYTHONUNBUFFERED: "1",
};

/**
 * E2E: 백엔드(FastAPI)와 웹(Vite)을 E2E 전용 포트로 직접 띄우고, 설치된 Chrome으로 화면을 조작한다.
 * 실행: `corepack pnpm e2e` (web 폴더). 서버는 테스트가 끝나면 Playwright가 종료한다.
 * 로그인: global-setup이 E2E 사용자를 준비하고, setup 프로젝트(auth.setup.ts)가 로그인해
 * 세션(storageState)을 저장한다. 나머지 테스트는 그 세션을 공유한다.
 * 로그인 화면 자체는 auth.spec.ts가 로그인하지 않은 빈 상태에서 확인한다.
 */
export default defineConfig({
  testDir: "./e2e",
  outputDir: "./e2e/.results",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  reporter: reporters,
  use: {
    baseURL: WEB_URL,
    channel: "chrome",
    locale: "ko-KR",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts$/ },
    {
      name: "chrome",
      dependencies: ["setup"],
      use: { storageState: STORAGE_STATE },
    },
  ],
  webServer: [
    {
      name: "backend",
      // 테스트 DB 스키마를 최신(alembic head)으로 올린 뒤 서버를 띄운다(pytest가 먼저 돌았으면 변화 없음).
      // psycopg async는 Windows Proactor 루프에서 동작하지 않아 Selector 루프로 띄운다
      // (--reload를 쓰는 개발 서버는 uvicorn이 이미 Selector 루프를 쓴다).
      command:
        `uv run rag-backend migrate && uv run uvicorn app.main:app --host 127.0.0.1 --port ${API_PORT}` +
        " --loop asyncio:SelectorEventLoop",
      cwd: "../backend",
      url: `${API_URL}/api/health`,
      env: BACKEND_ENV,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      name: "web",
      command: `corepack pnpm exec vite --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      url: WEB_URL,
      env: { API_TARGET: API_URL },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
