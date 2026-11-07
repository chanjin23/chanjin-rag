import { defineConfig } from "@playwright/test";
import { API_PORT, API_URL, BACKEND_LOG, WEB_PORT, WEB_URL } from "./e2e/env";

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
  reporter: [["list"], ["html", { outputFolder: "./e2e/.report", open: "never" }]],
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
      command: `uv run uvicorn app.main:app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: "../backend",
      url: `${API_URL}/api/health`,
      env: { LOG_FORMAT: "json", LOG_FILE: BACKEND_LOG, PYTHONUNBUFFERED: "1" },
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
