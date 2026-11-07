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
