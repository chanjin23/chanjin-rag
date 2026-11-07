import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { BACKEND_LOG } from "./env";

/** 실행마다 백엔드 로그를 비워, 이전 실행의 줄이 섞이지 않게 한다. */
export default function globalSetup(): void {
  mkdirSync(path.dirname(BACKEND_LOG), { recursive: true });
  writeFileSync(BACKEND_LOG, "");
}
