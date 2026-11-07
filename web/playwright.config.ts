import path from "node:path";
import { defineConfig, type ReporterDescription } from "@playwright/test";
import { API_PORT, API_URL, BACKEND_LOG, WEB_PORT, WEB_URL } from "./e2e/env";

// 루프 실행기가 LOOP_REPORT_DIR을 주면 JSON 리포트도 남긴다(필수 E2E를 제목으로 확인하는 데 씀).
const reporters: ReporterDescription[] = [
  ["list"],
  ["html", { outputFolder: "./e2e/.report", open: "never" }],
];
if (process.env.LOOP_REPORT_DIR) {
  reporters.push(["json", { outputFile: path.join(process.env.LOOP_REPORT_DIR, "e2e.json") }]);
}

// 테스트 DB(ParadeDB :5433). scripts/verify.sh가 띄우고 TEST_DATABASE_URL을 넘긴다.
const TEST_DATABASE_URL =
  process.env.TEST_DATABASE_URL ?? "postgresql+psycopg://rag:rag@127.0.0.1:5433/rag_test";

/**
 * E2E 백엔드 환경 변수.
 * - APP_ENV=test: 저장소 .env(실제 API 키)를 읽지 않는다. PROVIDER=fake: 모델 호출은 Fake Provider.
 * - LOG_QUERY_TEXT=true: 질문 원문을 로그에 남겨, 테스트가 이번 요청의 로그 줄을 찾을 수 있게 한다.
 * - LOG_FORMAT=json + LOG_FILE: 테스트가 읽는 JSON 로그 파일(e2e/backendLog.ts).
 */
const BACKEND_ENV = {
  APP_ENV: "test",
  PROVIDER: "fake",
  LOG_QUERY_TEXT: "true",
  LOG_FORMAT: "json",
  LOG_FILE: BACKEND_LOG,
  DATABASE_URL: TEST_DATABASE_URL,
  PYTHONUNBUFFERED: "1",
};

/**
 * E2E: 백엔드(FastAPI)와 웹(Vite)을 E2E 전용 포트로 직접 띄우고, 설치된 Chrome으로 화면을 조작한다.
 * 실행: `corepack pnpm e2e` (web 폴더). 서버는 테스트가 끝나면 Playwright가 종료한다.
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
  webServer: [
    {
      name: "backend",
      // 테스트 DB 스키마를 최신(alembic head)으로 올린 뒤 서버를 띄운다(pytest가 먼저 돌았으면 변화 없음).
      command: `uv run rag-backend migrate && uv run uvicorn app.main:app --host 127.0.0.1 --port ${API_PORT}`,
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
