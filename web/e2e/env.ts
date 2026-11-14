import path from "node:path";

/**
 * E2E 전용 포트와 경로. 개발 서버(8000, 5173)와 겹치지 않게 따로 둔다.
 * 개발 서버를 띄워 둔 채로 E2E를 돌려도 충돌하지 않는다.
 */
export const API_PORT = 8001;
export const WEB_PORT = 5174;
export const API_URL = `http://127.0.0.1:${API_PORT}`;
export const WEB_URL = `http://127.0.0.1:${WEB_PORT}`;

/** 백엔드가 JSON 한 줄씩 남기는 로그 파일. 테스트가 이 파일로 "요청을 받았는지" 확인한다. */
export const BACKEND_LOG = path.resolve(import.meta.dirname, ".logs", "backend.log");

/**
 * E2E 테스트 사용자. global-setup이 CLI(`rag-backend reset-password`/`create-user`)로 만들고,
 * setup 프로젝트(auth.setup.ts)가 로그인해 세션을 STORAGE_STATE에 저장해 다른 테스트가 공유한다.
 */
export const E2E_USER = "e2e";
export const E2E_PASSWORD = "e2e-password-1234";
export const STORAGE_STATE = path.resolve(import.meta.dirname, ".auth", "user.json");

/** 테스트 DB(ParadeDB :5433). scripts/verify.sh가 띄우고 TEST_DATABASE_URL을 넘긴다. */
export const TEST_DATABASE_URL =
  process.env.TEST_DATABASE_URL ?? "postgresql+psycopg://rag:rag@127.0.0.1:5433/rag_test";

/** 업로드 원본 임시 폴더(E2E 백엔드와 `rag-backend ingest-local`이 함께 씀). */
export const UPLOAD_TMP_DIR = path.resolve(import.meta.dirname, ".tmp", "uploads");

const IS_WINDOWS = process.platform === "win32";

/**
 * E2E 백엔드의 수집 허용 폴더(SYNC_ALLOWED_ROOT). Windows는 운영과 같은 `C:\Chanjin\dev`,
 * 그 밖(WSL 등 Linux)은 E2E 임시 폴더를 쓴다. Agent E2E는 이 아래에 실제 파일을 만들어 스캔하므로
 * 실행하는 OS에 실제로 있는 경로여야 한다. Sync API는 경로를 문자열로만 검사한다.
 */
export const SYNC_ROOT = IS_WINDOWS
  ? "C:\\Chanjin\\dev"
  : path.resolve(import.meta.dirname, ".tmp", "sync-root");

/** 허용 폴더 아래 경로(OS 구분자 사용). 예: `syncPath("project", "app", "README.md")` */
export function syncPath(...parts: string[]): string {
  return [SYNC_ROOT, ...parts].join(IS_WINDOWS ? "\\" : "/");
}
