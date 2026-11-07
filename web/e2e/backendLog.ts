import { existsSync, readFileSync } from "node:fs";
import { expect } from "@playwright/test";
import { BACKEND_LOG } from "./env";

export type LogRecord = { event: string; level: string; timestamp: string } & Record<string, unknown>;

export function readBackendLog(): LogRecord[] {
  if (!existsSync(BACKEND_LOG)) return [];
  return readFileSync(BACKEND_LOG, "utf-8")
    .split("\n")
    .filter((line) => line.startsWith("{"))
    .flatMap((line) => {
      try {
        return [JSON.parse(line) as LogRecord];
      } catch {
        return []; // 다른 프로세스가 쓰는 중인 줄은 건너뛴다
      }
    });
}

/** 지금까지 쌓인 로그 줄 수. `waitForBackendLog(match, { since })`로 이후 줄만 찾을 때 쓴다. */
export function backendLogMark(): number {
  return readBackendLog().length;
}

/** 조건에 맞는 줄을 모두 돌려준다(since: backendLogMark()로 얻은 위치 이후만). */
export function findBackendLogs(match: (r: LogRecord) => boolean, since = 0): LogRecord[] {
  return readBackendLog().slice(since).filter(match);
}

/** 조건에 맞는 로그 줄이 나타날 때까지 기다렸다가 돌려준다. */
export async function waitForBackendLog(
  match: (r: LogRecord) => boolean,
  { timeoutMs = 5_000, since = 0 }: { timeoutMs?: number; since?: number } = {},
): Promise<LogRecord> {
  let found: LogRecord | undefined;
  await expect
    .poll(
      () => {
        found = readBackendLog().slice(since).find(match);
        return found !== undefined;
      },
      { timeout: timeoutMs, message: `백엔드 로그(${BACKEND_LOG})에서 기대한 줄을 찾지 못함` },
    )
    .toBe(true);
  return found!;
}
