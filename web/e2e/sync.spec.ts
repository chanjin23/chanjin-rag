import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { type APIRequestContext, expect, test } from "@playwright/test";
import { backendLogMark, findBackendLogs, waitForBackendLog } from "./backendLog";
import { API_URL, BACKEND_LOG, TEST_DATABASE_URL, UPLOAD_TMP_DIR } from "./env";

/**
 * T07 Sync API(Agent 전용, Bearer 토큰): 실행 기록 → manifest 판정 → 업로드·메타 등록 → 완료.
 * Agent(T08)와 동기화 화면(T13)은 아직 없어서, Agent가 보낼 요청을 테스트가 직접 보내고
 * 응답과 백엔드 로그로 확인한다. 경로는 운영과 같은 Windows 경로 문자열(허용 폴더 C:\Chanjin\dev)이고,
 * 서버는 경로를 문자열로만 검사한다. 파일 내용은 모두 테스트가 만든 합성 텍스트.
 *
 * 테스트 DB는 E2E 실행 사이에 남으므로, 실행마다 새 폴더 이름(stamp)을 쓴다. manifest에는 이번
 * 테스트의 파일만 넣으므로 이전 문서는 "사라진 문서"가 되어 대량 삭제 보호에 걸린다(삭제되지 않음).
 */
const BACKEND_DIR = path.resolve(import.meta.dirname, "..", "..", "backend");
const DRAIN_ROOT = path.resolve(import.meta.dirname, ".tmp", "sync-drain");

function backendCli(args: string[], env: Record<string, string> = {}) {
  return spawnSync("uv", ["run", "rag-backend", ...args], {
    cwd: BACKEND_DIR,
    encoding: "utf-8",
    env: {
      ...process.env,
      APP_ENV: "test",
      PROVIDER: "fake",
      DATABASE_URL: TEST_DATABASE_URL,
      PYTHONIOENCODING: "utf-8",
      ...env,
    },
  });
}

/** Agent 토큰을 CLI로 발급한다(평문 토큰은 출력의 마지막 줄). */
function issueToken(): string {
  const res = backendCli(["create-token", `e2e-sync-${Date.now()}`]);
  expect(res.status, res.stderr).toBe(0);
  const lines = res.stdout.trim().split(/\r?\n/);
  return lines[lines.length - 1].trim();
}

/** 큐에 쌓인 작업을 Worker와 같은 실행기로 모두 처리한다(빈 폴더로 ingest-local 실행). */
function drainJobs() {
  mkdirSync(DRAIN_ROOT, { recursive: true });
  return backendCli(["ingest-local", DRAIN_ROOT], {
    SYNC_ALLOWED_ROOT: DRAIN_ROOT,
    UPLOAD_TMP_DIR,
    LOG_FORMAT: "json",
    LOG_FILE: BACKEND_LOG,
  });
}

const sha256 = (data: string | Buffer) => createHash("sha256").update(data).digest("hex");
const MTIME = "2026-11-08T09:00:00+09:00";

class Agent {
  constructor(
    private request: APIRequestContext,
    private token: string,
  ) {}

  private get headers() {
    return { Authorization: `Bearer ${this.token}` };
  }

  async get(url: string) {
    return this.request.get(`${API_URL}/api/sync${url}`, { headers: this.headers });
  }

  async post(url: string, data?: unknown) {
    return this.request.post(`${API_URL}/api/sync${url}`, { headers: this.headers, data });
  }

  async startRun(): Promise<number> {
    const res = await this.post("/runs", { source: "cli" });
    expect(res.status()).toBe(201);
    return (await res.json()).run_id as number;
  }

  async manifest(runId: number, files: object[], scanErrors: string[] = []) {
    const res = await this.post(`/runs/${runId}/manifest`, { files, scan_errors: scanErrors });
    expect(res.status()).toBe(200);
    return res.json();
  }

  async upload(filePath: string, content: string, sha = sha256(content), runId?: number) {
    return this.request.put(`${API_URL}/api/sync/files`, {
      headers: this.headers,
      multipart: {
        path: filePath,
        sha256: sha,
        mtime: MTIME,
        ...(runId === undefined ? {} : { run_id: String(runId) }),
        file: { name: "upload.bin", mimeType: "application/octet-stream", buffer: Buffer.from(content) },
      },
    });
  }
}

const entry = (filePath: string, sha: string, size = 100) => ({ path: filePath, size, mtime: MTIME, sha256: sha });

test("Sync API: manifest 판정에 따라 업로드·메타 등록을 받고 실행 완료를 기록한다", async ({ request }) => {
  test.setTimeout(120_000);
  const stamp = `${Date.now()}`;
  const folder = `C:\\Chanjin\\dev\\e2e-sync-${stamp}`;
  const memo = `${folder}\\회의록.md`;
  const big = `${folder}\\설계도면.pdf`;
  const memoText = `# 동기화 ${stamp}\n\n다음 배포는 금요일에 한다.\n`;
  const bigSha = sha256(`big-${stamp}`);

  // 토큰이 없으면 거부
  const anonymous = await request.get(`${API_URL}/api/sync/config`);
  expect(anonymous.status()).toBe(401);
  expect((await anonymous.json()).error.code).toBe("invalid_token");

  const agent = new Agent(request, issueToken());
  const since = backendLogMark();

  const config = await (await agent.get("/config")).json();
  expect(config.max_file_mb).toBe(10);
  expect(config.excludes.filter((e: { fixed: boolean }) => e.fixed).map((e: { pattern: string }) => e.pattern)).toEqual([
    "/project/",
    "/backup/",
  ]);

  const runId = await agent.startRun();
  await waitForBackendLog((r) => r.event === "sync.run.started" && r.run_id === runId, { since });

  const judged = await agent.manifest(runId, [
    entry(memo, sha256(memoText), Buffer.byteLength(memoText)),
    entry(big, bigSha, 11 * 1024 * 1024),
    entry("C:\\Chanjin\\dev\\project\\app\\README.md", sha256("x")),
  ]);
  expect(judged.upload).toEqual([memo]);
  expect(judged.meta_only).toEqual([big]);
  expect(judged.rejected).toEqual([{ path: "C:\\Chanjin\\dev\\project\\app\\README.md", reason: "fixed_exclude" }]);
  const judgedLog = await waitForBackendLog((r) => r.event === "sync.manifest.judged" && r.run_id === runId, { since });
  expect(judgedLog).toMatchObject({ files: 3, upload: 1, meta_only: 1, rejected: 1 });

  // 판정과 업로드 사이에 파일이 바뀌면 409 hash_mismatch
  const mismatch = await agent.upload(memo, memoText, sha256("manifest 때의 내용"), runId);
  expect(mismatch.status()).toBe(409);
  expect((await mismatch.json()).error.code).toBe("hash_mismatch");

  // 고정 제외 경로는 서버가 다시 검증해 거부
  const excluded = await agent.upload("C:\\Chanjin\\dev\\backup\\rag.md", memoText);
  expect(excluded.status()).toBe(400);
  expect((await excluded.json()).error.code).toBe("fixed_exclude");

  const uploaded = await agent.upload(memo, memoText, undefined, runId);
  expect(uploaded.status()).toBe(200);
  const { document_id: documentId, outcome } = await uploaded.json();
  expect(outcome).toBe("queued");
  // 업로드 로그는 실행 ID와 해시 앞 8자리로 추적한다(경로·파일명 없음)
  const uploadedLog = await waitForBackendLog(
    (r) => r.event === "sync.file.uploaded" && r.document_id === documentId,
    { since },
  );
  expect(uploadedLog.run_id).toBe(runId);
  expect(uploadedLog.sha256_prefix).toBe(sha256(memoText).slice(0, 8));
  await waitForBackendLog((r) => r.event === "sync.file.hash_mismatch" && r.run_id === runId, { since });

  const meta = await agent.post("/files/meta", { run_id: runId, files: [entry(big, bigSha, 11 * 1024 * 1024)] });
  expect(meta.status()).toBe(200);
  expect((await meta.json()).accepted[0].outcome).toBe("meta_only");

  expect((await agent.post(`/runs/${runId}/heartbeat`)).status()).toBe(200);
  const complete = await agent.post(`/runs/${runId}/complete`, {
    status: "succeeded",
    stats: { uploaded: 1, meta_only: 1, skipped_formats: { ".doc": 1 } },
  });
  expect(complete.status()).toBe(200);
  expect((await complete.json()).status).toBe("succeeded");
  await waitForBackendLog((r) => r.event === "sync.run.completed" && r.run_id === runId, { since });

  // 업로드로 등록된 extract 작업을 Worker 실행기로 처리하면 검색 가능한 상태가 된다
  const drained = drainJobs();
  expect(drained.status, drained.stderr).toBe(0);
  const embedded = await waitForBackendLog(
    (r) => r.event === "job.completed" && r.type === "embed" && r.document_id === documentId,
    { since },
  );
  expect(embedded.status).toBe("ready");

  // 로그에는 경로·파일명이 남지 않는다
  const syncLogs = findBackendLogs((r) => r.event.startsWith("sync."), since);
  expect(JSON.stringify(syncLogs)).not.toContain("회의록");
});

test("Sync API: 이동은 경로만 바꾸고, 한 번에 많이 사라지면 삭제 판정을 보류한다", async ({ request }) => {
  test.setTimeout(120_000);
  const stamp = `${Date.now()}`;
  const folder = `C:\\Chanjin\\dev\\e2e-sync-${stamp}`;
  const before = `${folder}\\메모.md`;
  const after = `${folder}\\보관\\메모-이동.md`;
  const big = `${folder}\\큰자료.pdf`;
  const text = `# 이동 ${stamp}\n\n이 파일은 폴더를 옮긴다.\n`;
  const bigSha = sha256(`big-${stamp}`);
  const agent = new Agent(request, issueToken());

  // 1회차: 새 파일 업로드와 큰 파일 메타 등록
  const first = await agent.startRun();
  const judged = await agent.manifest(first, [entry(before, sha256(text)), entry(big, bigSha, 20 * 1024 * 1024)]);
  expect(judged.upload).toEqual([before]);
  const up = await agent.upload(before, text);
  expect(up.status()).toBe(200);
  const documentId = (await up.json()).document_id;
  await agent.post("/files/meta", { files: [entry(big, bigSha, 20 * 1024 * 1024)] });
  await agent.post(`/runs/${first}/complete`, { status: "succeeded" });

  // 2회차: 메모를 다른 폴더로 옮기고 큰 파일은 사라짐
  const since = backendLogMark();
  const second = await agent.startRun();
  const moved = await agent.manifest(second, [entry(after, sha256(text))]);
  expect(moved.moved).toEqual([{ from: before, to: after }]);
  expect(moved.upload).toEqual([]);
  expect(moved.deleted).toEqual([]);
  expect(moved.deletion_held).toContain(big);
  const held = await waitForBackendLog((r) => r.event === "sync.deletion.held" && r.run_id === second, { since });
  expect(held.reason).toBe("mass_delete_guard");
  expect(held.count as number).toBeGreaterThanOrEqual(1);
  await waitForBackendLog((r) => r.event === "sync.manifest.judged" && r.run_id === second && r.moved === 1, { since });
  await agent.post(`/runs/${second}/complete`, { status: "succeeded" });

  // 이동한 문서는 같은 문서 ID로 처리된다(새 업로드 없음)
  const drained = drainJobs();
  expect(drained.status, drained.stderr).toBe(0);
  await waitForBackendLog(
    (r) => r.event === "job.completed" && r.type === "extract" && r.document_id === documentId,
    { since },
  );
  expect(findBackendLogs((r) => r.event === "sync.file.uploaded", since)).toHaveLength(0);
});
