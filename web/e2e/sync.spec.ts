import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { type APIRequestContext, expect, type Page, test } from "@playwright/test";
import { backendLogMark, findBackendLogs, waitForBackendLog } from "./backendLog";
import { API_URL, BACKEND_LOG, SYNC_ROOT, syncPath, TEST_DATABASE_URL, UPLOAD_TMP_DIR } from "./env";

/**
 * T07 Sync API(Agent 전용, Bearer 토큰): 실행 기록 → manifest 판정 → 업로드·메타 등록 → 완료.
 * Agent(T08)와 동기화 화면(T13)은 아직 없어서, Agent가 보낼 요청을 테스트가 직접 보내고
 * 응답과 백엔드 로그로 확인한다. 경로는 E2E 허용 폴더(env.ts `SYNC_ROOT`, Windows는 운영과 같은 C:\Chanjin\dev)
 * 아래 문자열이고, 서버는 경로를 문자열로만 검사한다. 파일 내용은 모두 테스트가 만든 합성 텍스트.
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
  const folder = syncPath(`e2e-sync-${stamp}`);
  const memo = syncPath(`e2e-sync-${stamp}`, "회의록.md");
  const big = syncPath(`e2e-sync-${stamp}`, "설계도면.pdf");
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
    entry(syncPath("project", "app", "README.md"), sha256("x")),
  ]);
  expect(judged.upload).toEqual([memo]);
  expect(judged.meta_only).toEqual([big]);
  expect(judged.rejected).toEqual([{ path: syncPath("project", "app", "README.md"), reason: "fixed_exclude" }]);
  const judgedLog = await waitForBackendLog((r) => r.event === "sync.manifest.judged" && r.run_id === runId, { since });
  expect(judgedLog).toMatchObject({ files: 3, upload: 1, meta_only: 1, rejected: 1 });

  // 판정과 업로드 사이에 파일이 바뀌면 409 hash_mismatch
  const mismatch = await agent.upload(memo, memoText, sha256("manifest 때의 내용"), runId);
  expect(mismatch.status()).toBe(409);
  expect((await mismatch.json()).error.code).toBe("hash_mismatch");

  // 고정 제외 경로는 서버가 다시 검증해 거부
  const excluded = await agent.upload(syncPath("backup", "rag.md"), memoText);
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
  const folder = syncPath(`e2e-sync-${stamp}`);
  const before = syncPath(`e2e-sync-${stamp}`, "메모.md");
  const after = syncPath(`e2e-sync-${stamp}`, "보관", "메모-이동.md");
  const big = syncPath(`e2e-sync-${stamp}`, "큰자료.pdf");
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

// ---------------------------------------------------------------------------
// T13 동기화 화면(/sync): 상태, 범위 설정, 지금 동기화
// ---------------------------------------------------------------------------
//
// - "지금 동기화"는 Windows에서만 보이므로 Windows 브라우저로 보이게 한다(userAgent + userAgentData).
// - 실행 기록은 위의 Agent 도우미(Bearer 토큰)로 만든다. 실제 Agent는 E2E에 설치되어 있지 않으므로
//   chanjin-sync 링크를 눌러도 새 실행이 생기지 않는다(30초 뒤 CLI 안내).
// - 시간(60초 비활성화, 30초 대기, 2초 간격 조회)은 page.clock으로 앞당긴다.

const WINDOWS_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36";
test.use({ userAgent: WINDOWS_UA });

async function openSyncPage(page: Page) {
  await page.addInitScript(() => {
    Object.defineProperty(Navigator.prototype, "userAgentData", {
      configurable: true,
      get: () => ({ platform: "Windows", mobile: false, brands: [] }),
    });
  });
  await page.goto("/sync");
  await expect(page.getByRole("heading", { name: "동기화", level: 1 })).toBeVisible();
}

test("범위를 줄이면 삭제될 문서 수를 확인받는다", async ({ page, request }) => {
  test.setTimeout(60_000);
  const stamp = `${Date.now()}`;
  const folder = `e2e-scope-${stamp}`;
  const pattern = `${folder}/`;
  const agent = new Agent(request, issueToken());
  const ids: number[] = [];
  for (const name of ["범위메모1.md", "범위메모2.md"]) {
    const text = `# 범위 ${stamp} ${name}\n\n범위에서 빠질 합성 문서.\n`;
    const res = await agent.upload(syncPath(folder, name), text);
    expect(res.status()).toBe(200);
    ids.push((await res.json()).document_id as number);
  }

  const since = backendLogMark();
  await openSyncPage(page);
  const scope = page.getByRole("region", { name: "수집 범위" });
  await scope.getByLabel("제외 패턴 추가").fill(pattern);
  await scope.getByRole("button", { name: "추가" }).nth(1).click();
  await expect(scope.getByRole("list", { name: "제외 패턴" })).toContainText(pattern);
  await scope.getByRole("button", { name: "저장", exact: true }).click();

  // 저장 전 확인 창: 삭제 수, 예시, 백업·외부 전송 안내
  const dialog = page.getByRole("alertdialog");
  await expect(dialog.getByTestId("purge-count")).toHaveText("범위에서 빠지는 문서 2개를 서버에서 영구 삭제합니다.");
  await expect(dialog.getByRole("list", { name: "삭제될 문서 예시" }).getByRole("listitem")).toHaveCount(2);
  await expect(dialog).toContainText(folder);
  await expect(dialog).toContainText("백업 파일에는 최대 7일(PC)까지 남습니다");
  await expect(dialog).toContainText("이미 NVIDIA API로 보낸 텍스트는 되돌릴 수 없습니다");
  const previewed = await waitForBackendLog((r) => r.event === "settings.sync.previewed", { since });
  expect(previewed.count).toBe(2);

  // 확인하면 저장하고 범위에서 빠진 문서를 영구 삭제한다
  await dialog.getByRole("button", { name: "2개 삭제하고 저장" }).click();
  await expect(dialog).toBeHidden();
  await expect(scope.getByRole("status")).toHaveText("저장했습니다. 범위에서 빠진 문서 2개를 삭제했습니다.");
  const updated = await waitForBackendLog((r) => r.event === "settings.sync.updated", { since });
  expect(updated).toMatchObject({ purged: 2, excludes_added: 1 });
  const purged = await waitForBackendLog((r) => r.event === "documents.purged" && r.reason === "scope", { since });
  expect(purged.documents).toBe(2);
  // 로그에는 경로·패턴 원문이 남지 않는다
  expect(JSON.stringify(findBackendLogs((r) => r.event.startsWith("settings."), since))).not.toContain(folder);

  // 문서 화면에서도 사라진다
  const gone = await page.request.get(`/api/documents/${ids[0]}`);
  expect(gone.status()).toBe(404);

  // 패턴을 다시 빼서 원래 범위로 되돌린다(삭제되는 문서 0개)
  const restoreSince = backendLogMark();
  await scope.getByRole("button", { name: `${pattern} 삭제` }).click();
  await scope.getByRole("button", { name: "저장", exact: true }).click();
  await expect(dialog.getByTestId("purge-count")).toContainText("삭제되는 문서 0개");
  await dialog.getByRole("button", { name: "저장", exact: true }).click();
  await expect(scope.getByRole("status")).toHaveText("저장했습니다. 삭제된 문서는 없습니다.");
  const restored = await waitForBackendLog((r) => r.event === "settings.sync.updated", { since: restoreSince });
  expect(restored).toMatchObject({ purged: 0, excludes_removed: 1 });
});

test("고정 제외는 편집할 수 없다", async ({ page }) => {
  const since = backendLogMark();
  await openSyncPage(page);
  await waitForBackendLog((r) => r.event === "settings.sync.read", { since });
  const scope = page.getByRole("region", { name: "수집 범위" });

  // 고정 제외는 읽기 전용으로 보이고 삭제 버튼이 없다
  const fixed = scope.getByTestId("fixed-exclude");
  await expect(fixed).toHaveCount(2);
  await expect(fixed.nth(0)).toContainText("/project/");
  await expect(fixed.nth(1)).toContainText("/backup/");
  await expect(fixed.nth(0)).toContainText("고정 · 편집 불가");
  await expect(fixed.getByRole("button")).toHaveCount(0);
  // 편집 가능한 패턴은 삭제할 수 있다
  await expect(scope.getByRole("button", { name: ".git/ 삭제" })).toBeVisible();

  // 고정 제외를 편집 목록에 넣으려 하면 막는다
  await scope.getByLabel("제외 패턴 추가").fill("/project/");
  await scope.getByRole("button", { name: "추가" }).nth(1).click();
  await expect(scope.getByRole("alert")).toHaveText("고정 제외는 이미 적용되어 있어 편집할 수 없습니다");
  await expect(scope.getByRole("list", { name: "제외 패턴" })).not.toContainText("/project/");
  await expect(scope.getByRole("button", { name: "저장", exact: true })).toBeDisabled();
});

test("실행 중이면 지금 동기화 대신 진행 상태가 보인다", async ({ page, request }) => {
  const agent = new Agent(request, issueToken());
  const since = backendLogMark();
  const runId = await agent.startRun();
  try {
    await waitForBackendLog((r) => r.event === "sync.run.started" && r.run_id === runId, { since });
    await openSyncPage(page);
    const progress = page.getByTestId("sync-progress");
    await expect(progress).toContainText("동기화 진행 중");
    await expect(progress).toContainText("CLI에서 시작");
    await expect(page.getByRole("button", { name: "지금 동기화" })).toHaveCount(0);
  } finally {
    const done = await agent.post(`/runs/${runId}/complete`, { status: "succeeded" });
    expect(done.status()).toBe(200);
  }
  await waitForBackendLog((r) => r.event === "sync.run.completed" && r.run_id === runId, { since });
  // 2초 간격 조회로 실행이 끝난 것을 보고 버튼이 돌아온다
  await expect(page.getByRole("button", { name: "지금 동기화" })).toBeEnabled({ timeout: 10_000 });
  await expect(page.getByTestId("sync-progress")).toHaveCount(0);
  await expect(page.getByTestId("latest-run")).toContainText("성공");
});

test("지금 동기화를 누르면 60초 동안 비활성화된다", async ({ page }) => {
  await page.clock.install();
  await openSyncPage(page);
  const button = page.getByRole("button", { name: "지금 동기화" });
  await expect(button).toBeEnabled();
  await expect(page.getByTestId("sync-run-link")).toHaveAttribute("href", "chanjin-sync://run?source=web");

  const since = backendLogMark();
  await button.click();
  await expect(button).toBeDisabled();
  await expect(page.getByText("Agent 실행을 기다리는 중입니다…")).toBeVisible();
  const requested = await waitForBackendLog((r) => r.event === "sync.run.requested", { since });
  expect(requested.source).toBe("web");

  await page.clock.runFor(58_000);
  await expect(button).toBeDisabled();
  await page.clock.runFor(3_000);
  await expect(button).toBeEnabled();
});

test("30초 안에 실행이 없으면 CLI 안내가 보인다", async ({ page }) => {
  await page.clock.install();
  await openSyncPage(page);
  const since = backendLogMark();
  await page.getByRole("button", { name: "지금 동기화" }).click();
  await waitForBackendLog((r) => r.event === "sync.run.requested", { since });

  // 30초 전에는 기다리는 중(2초 간격으로 상태를 조회)
  await page.clock.runFor(28_000);
  await expect(page.getByText("Agent 실행을 기다리는 중입니다…")).toBeVisible();
  await expect(page.getByTestId("cli-hint")).toHaveCount(0);

  await page.clock.runFor(3_000);
  const hint = page.getByTestId("cli-hint");
  await expect(hint).toContainText("Agent가 설치되지 않았거나 브라우저가 실행을 막았습니다");
  await expect(hint).toContainText("rag-agent sync");
  await expect(page.getByText("Agent 실행을 기다리는 중입니다…")).toHaveCount(0);
  // 아직 60초 전이라 버튼은 비활성
  await expect(page.getByRole("button", { name: "지금 동기화" })).toBeDisabled();
});

test("보류된 삭제 판정을 적용할 수 있다", async ({ page, request }) => {
  test.setTimeout(60_000);
  const stamp = `${Date.now()}`;
  const agent = new Agent(request, issueToken());
  // 범위 안의 활성 문서가 하나는 있게 한다
  const text = `# 보류 ${stamp}\n\n보류 판정용 합성 문서.\n`;
  expect((await agent.upload(syncPath(`e2e-held-${stamp}`, "보류.md"), text)).status()).toBe(200);

  // 파일이 하나도 없는 manifest → 모두 사라진 것으로 보여 대량 삭제 보호로 보류
  const since = backendLogMark();
  const runId = await agent.startRun();
  const judged = await agent.manifest(runId, []);
  expect(judged.deleted).toEqual([]);
  expect(judged.deletion_held.length).toBeGreaterThan(0);
  await agent.post(`/runs/${runId}/complete`, { status: "succeeded" });
  await waitForBackendLog((r) => r.event === "sync.deletion.held" && r.run_id === runId, { since });

  try {
    await openSyncPage(page);
    const panel = page.getByRole("alert", { name: "보류된 삭제 판정" });
    await expect(panel).toContainText(`${judged.deletion_held.length}개(100%)가 사라져`);
    await panel.getByRole("button", { name: "삭제 판정 적용" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("보류된 삭제 판정을 적용할까요?");
    await dialog.getByRole("button", { name: "적용" }).click();
    await expect(dialog).toBeHidden();
    await expect(panel).toContainText("적용을 승인했습니다. 다음 동기화 때 삭제 판정을 적용합니다.");
    await expect(panel.getByRole("button", { name: "삭제 판정 적용" })).toHaveCount(0);
    const applied = await waitForBackendLog((r) => r.event === "sync.held_deletions.applied", { since });
    expect(applied).toMatchObject({ run_id: runId, count: judged.deletion_held.length });
  } finally {
    // 정리: 다음 실행이 승인을 쓰되, 허용 폴더 전체를 "읽지 못함"으로 보내 아무것도 삭제하지 않는다
    // (다른 E2E가 쓰는 문서를 지우지 않게)
    const cleanup = await agent.startRun();
    const result = await agent.manifest(cleanup, [], [SYNC_ROOT]);
    expect(result.deleted).toEqual([]);
    await agent.post(`/runs/${cleanup}/complete`, { status: "succeeded" });
  }
  await waitForBackendLog((r) => r.event === "sync.deletion.approval_used", { since });
  // 판정을 받은 최근 실행에 대량 보류가 없으므로 경고가 사라진다
  await page.reload();
  await expect(page.getByRole("heading", { name: "동기화", level: 1 })).toBeVisible();
  await expect(page.getByTestId("latest-run")).toBeVisible();
  await expect(page.getByRole("alert", { name: "보류된 삭제 판정" })).toHaveCount(0);
});
