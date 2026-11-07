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
