import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { backendLogMark, findBackendLogs, waitForBackendLog } from "./backendLog";
import { BACKEND_LOG, TEST_DATABASE_URL } from "./env";

/**
 * T06 작업 큐와 Ingestion Worker: 개발·E2E 시드용 CLI `rag-backend ingest-local <폴더>`가
 * 업로드와 같은 경로로 큐에 넣고 extract → index → embed 작업을 처리하는지 백엔드 로그로 확인한다.
 * 수집 허용 폴더(SYNC_ALLOWED_ROOT)는 E2E 임시 폴더로 바꿔, 범위 검증을 그대로 적용한다.
 * 문서 화면은 아직 없으므로(T08) 화면 대신 로그 이벤트와 CLI 출력으로 확인한다.
 */
const ROOT = path.resolve(import.meta.dirname, ".tmp", "ingest-root");
const UPLOADS = path.resolve(import.meta.dirname, ".tmp", "uploads");

function ingestLocal(folder: string) {
  return spawnSync("uv", ["run", "rag-backend", "ingest-local", folder], {
    cwd: path.resolve(import.meta.dirname, "..", "..", "backend"),
    encoding: "utf-8",
    env: {
      ...process.env,
      APP_ENV: "test",
      PROVIDER: "fake",
      DATABASE_URL: TEST_DATABASE_URL,
      LOG_FORMAT: "json",
      LOG_FILE: BACKEND_LOG,
      SYNC_ALLOWED_ROOT: ROOT,
      UPLOAD_TMP_DIR: UPLOADS,
      PYTHONIOENCODING: "utf-8",
    },
  });
}

test("ingest-local로 넣은 폴더가 Worker 작업(extract, index, embed)으로 처리된다", async () => {
  test.setTimeout(120_000);
  rmSync(ROOT, { recursive: true, force: true });
  // 실행마다 새 폴더라(테스트 DB는 E2E 실행 사이에 남음) 항상 새 문서로 접수된다
  const stamp = `${Date.now()}`;
  const folder = path.join(ROOT, `notes-${stamp}`);
  mkdirSync(folder, { recursive: true });
  writeFileSync(path.join(folder, "회의록.md"), `# 회의 ${stamp}\n\n다음 배포는 금요일에 한다.\n`);
  writeFileSync(path.join(folder, "빈파일.txt"), "  \n\n");
  writeFileSync(path.join(folder, "매출.xlsx"), "not parsed yet");

  const since = backendLogMark();
  const res = ingestLocal(folder);
  expect(res.status, res.stderr).toBe(0);
  expect(res.stdout).toContain("접수: 새 버전 3");

  const done = await waitForBackendLog((r) => r.event === "ingest.local.completed", { since });
  expect(done.queued).toBe(3);
  expect(done.statuses).toEqual({ ready: 1, no_text: 1, failed: 1 });

  // 작업 로그: extract 3개, 그중 md만 index → embed까지
  for (const type of ["extract", "index", "embed"]) {
    await waitForBackendLog((r) => r.event === "job.started" && r.type === type, { since });
  }
  const completed = findBackendLogs((r) => r.event === "job.completed", since);
  expect(completed.filter((r) => r.type === "extract")).toHaveLength(3);
  const embed = completed.find((r) => r.type === "embed");
  expect(embed?.status).toBe("ready");
  const indexed = completed.find((r) => r.type === "index");
  expect(indexed?.document_id).toBe(embed?.document_id);
  const unsupported = completed.find((r) => r.failure_code === "unsupported_format");
  expect(unsupported?.status).toBe("failed");
  // 로그에는 경로·파일명이 남지 않는다
  expect(JSON.stringify(completed)).not.toContain("회의록");
});

test("ingest-local은 수집 허용 폴더 밖을 거부한다", async () => {
  test.setTimeout(60_000);
  const outside = path.resolve(import.meta.dirname, ".tmp", "outside");
  mkdirSync(outside, { recursive: true });
  writeFileSync(path.join(outside, "a.md"), "# 밖\n\n범위 밖");

  const since = backendLogMark();
  const res = ingestLocal(outside);
  expect(res.status).toBe(1);
  expect(res.stderr).toContain("수집 범위 밖");
  expect(findBackendLogs((r) => r.event === "ingest.local.started", since)).toHaveLength(0);
});
