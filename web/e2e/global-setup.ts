import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { BACKEND_LOG, E2E_PASSWORD, E2E_USER, STORAGE_STATE, TEST_DATABASE_URL } from "./env";

/** 백엔드 CLI를 실행한다. 비밀번호는 명령줄이 아니라 표준 입력으로 넘긴다. */
function backendCli(args: string[], input: string) {
  return spawnSync("uv", ["run", "rag-backend", ...args], {
    cwd: path.resolve(import.meta.dirname, "..", "..", "backend"),
    input,
    encoding: "utf-8",
    env: { ...process.env, APP_ENV: "test", PROVIDER: "fake", DATABASE_URL: TEST_DATABASE_URL },
  });
}

/**
 * Playwright는 webServer(백엔드: 마이그레이션 후 기동)를 띄운 뒤 이 함수를 부른다.
 * 1) 실행마다 백엔드 로그를 비워, 이전 실행의 줄이 섞이지 않게 한다.
 * 2) E2E 사용자를 준비한다. 있으면 비밀번호를 다시 정해 잠금·세션을 초기화하고, 없으면 만든다.
 */
export default function globalSetup(): void {
  mkdirSync(path.dirname(BACKEND_LOG), { recursive: true });
  writeFileSync(BACKEND_LOG, "");
  rmSync(STORAGE_STATE, { force: true });

  const reset = backendCli(["reset-password", E2E_USER, "--password-stdin"], `${E2E_PASSWORD}\n`);
  if (reset.status === 0) return;
  const create = backendCli(["create-user", E2E_USER, "--password-stdin"], `${E2E_PASSWORD}\n`);
  if (create.status !== 0) {
    throw new Error(
      `E2E 사용자를 준비하지 못했습니다.\nreset-password: ${reset.stderr}\ncreate-user: ${create.stderr}`,
    );
  }
}
