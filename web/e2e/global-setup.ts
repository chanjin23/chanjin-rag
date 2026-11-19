import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { BACKEND_LOG, E2E_PASSWORD, E2E_USER, STORAGE_STATE, SYNC_ROOT, TEST_DATABASE_URL } from "./env";

const BACKEND_DIR = path.resolve(import.meta.dirname, "..", "..", "backend");

/**
 * 수집 루트(`sync_config.roots`)를 E2E 허용 폴더(SYNC_ROOT) 하나로 맞춘다. 마이그레이션은 운영 값
 * `C:\Chanjin\dev`를 넣으므로, Linux(WSL)에서는 이렇게 맞춰야 Sync·Agent E2E 경로가 루트 안에 든다.
 * Windows에서는 같은 값이라 바뀌는 것이 없다.
 */
function resetSyncRoots(): void {
  const code = [
    "import json, os, psycopg",
    "url = os.environ['DATABASE_URL'].replace('+psycopg', '')",
    "with psycopg.connect(url) as conn:",
    "    conn.execute('UPDATE sync_config SET roots = %s::jsonb WHERE id = 1',",
    "                 (json.dumps([os.environ['SYNC_ROOT']]),))",
  ].join("\n");
  const res = spawnSync("uv", ["run", "python", "-c", code], {
    cwd: BACKEND_DIR,
    encoding: "utf-8",
    env: { ...process.env, DATABASE_URL: TEST_DATABASE_URL, SYNC_ROOT },
  });
  if (res.status !== 0) {
    throw new Error(`sync_config.roots를 E2E 허용 폴더로 맞추지 못했습니다.\n${res.stderr}`);
  }
}

/** 백엔드 CLI를 실행한다. 비밀번호는 명령줄이 아니라 표준 입력으로 넘긴다. */
function backendCli(args: string[], input: string) {
  return spawnSync("uv", ["run", "rag-backend", ...args], {
    cwd: BACKEND_DIR,
    input,
    encoding: "utf-8",
    env: { ...process.env, APP_ENV: "test", PROVIDER: "fake", DATABASE_URL: TEST_DATABASE_URL },
  });
}

/**
 * Playwright는 webServer(백엔드: 마이그레이션 후 기동)를 띄운 뒤 이 함수를 부른다.
 * 1) 실행마다 백엔드 로그를 비워, 이전 실행의 줄이 섞이지 않게 한다.
 * 2) 수집 루트를 E2E 허용 폴더로 맞춘다(resetSyncRoots).
 * 3) E2E 사용자를 준비한다. 있으면 비밀번호를 다시 정해 잠금·세션을 초기화하고, 없으면 만든다.
 */
export default function globalSetup(): void {
  mkdirSync(path.dirname(BACKEND_LOG), { recursive: true });
  writeFileSync(BACKEND_LOG, "");
  rmSync(STORAGE_STATE, { force: true });
  mkdirSync(SYNC_ROOT, { recursive: true });
  resetSyncRoots();

  const reset = backendCli(["reset-password", E2E_USER, "--password-stdin"], `${E2E_PASSWORD}\n`);
  if (reset.status === 0) return;
  const create = backendCli(["create-user", E2E_USER, "--password-stdin"], `${E2E_PASSWORD}\n`);
  if (create.status !== 0) {
    throw new Error(
      `E2E 사용자를 준비하지 못했습니다.\nreset-password: ${reset.stderr}\ncreate-user: ${create.stderr}`,
    );
  }
}
