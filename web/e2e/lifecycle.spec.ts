import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { backendLogMark, findBackendLogs, waitForBackendLog } from "./backendLog";
import { API_URL, BACKEND_LOG, BACKUP_DIR, syncPath, TEST_DATABASE_URL, UPLOAD_TMP_DIR } from "./env";

/**
 * T14 데이터 수명주기 (F-19, PRD 3장 DB 백업·서버 데이터 삭제, TRD 6.5, 10.3, 12.2).
 *
 * - 백업: Worker의 매일 03:00 작업과 같은 코드를 `rag-backend backup`으로 실행한다. WSL·CI에는 pg_dump·age가
 *   없으므로 설정값 `BACKUP_PG_DUMP_CMD`·`BACKUP_AGE_CMD`에 가짜 명령(e2e/fixtures/*.mjs, Node로 실행)을 준다.
 *   만든 백업은 E2E 백엔드가 같은 `BACKUP_DIR`에서 `/sync/backups*`로 내려준다.
 * - Agent: 실제 `rag-agent sync`를 돌려 동기화 뒤 없는 백업만 내려받고 SHA-256을 검증하는지 본다.
 *   `RAG_AGENT_BACKUP_DIR`은 e2e/.tmp 아래(실제 백업 폴더를 건드리지 않음).
 * - 30일 삭제: `rag-backend ingest-local`로 합성 문서를 넣고, 로컬 삭제 시각을 31일 전으로 바꾼 뒤
 *   매일 04:00 작업과 같은 `rag-backend purge-retention`을 실행한다.
 */
const REPO_DIR = path.resolve(import.meta.dirname, "..", "..");
const BACKEND_DIR = path.join(REPO_DIR, "backend");
const AGENT_DIR = path.join(REPO_DIR, "agent");
const FIXTURES = path.resolve(import.meta.dirname, "fixtures");
const ROOT = path.resolve(import.meta.dirname, ".tmp", "lifecycle-root");
const AGE_RECIPIENT = "age1e2efakerecipient0000000000000000000000000000000000000000000";

function backendCli(args: string[], env: Record<string, string> = {}) {
  return spawnSync("uv", ["run", "rag-backend", ...args], {
    cwd: BACKEND_DIR,
    encoding: "utf-8",
    env: {
      ...process.env,
      APP_ENV: "test",
      PROVIDER: "fake",
      DATABASE_URL: TEST_DATABASE_URL,
      LOG_FORMAT: "json",
      LOG_FILE: BACKEND_LOG,
      PYTHONIOENCODING: "utf-8",
      ...env,
    },
  });
}

/** DB에서 문서 하나를 "로컬 삭제됨"으로, 삭제 시각을 `days`일 전으로 바꾼다(시간이 흐른 것을 흉내). */
function markLocallyDeleted(filename: string, days: number): void {
  const code = [
    "import os, psycopg",
    "url = os.environ['DATABASE_URL'].replace('+psycopg', '')",
    "with psycopg.connect(url) as conn:",
    "    cur = conn.execute(\"UPDATE documents SET locally_deleted = true, deleted_at = now() - make_interval(days => %s) WHERE filename = %s\",",
    "                       (int(os.environ['DAYS']), os.environ['FILENAME']))",
    "    assert cur.rowcount == 1, cur.rowcount",
  ].join("\n");
  const res = spawnSync("uv", ["run", "python", "-c", code], {
    cwd: BACKEND_DIR,
    encoding: "utf-8",
    env: { ...process.env, DATABASE_URL: TEST_DATABASE_URL, FILENAME: filename, DAYS: String(days) },
  });
  expect(res.status, res.stderr).toBe(0);
}

function codeName(seed: number): string {
  let n = seed;
  let out = "";
  while (n > 0) {
    out += String.fromCharCode(97 + (n % 26));
    n = Math.floor(n / 26);
  }
  return `zl${out}`;
}

const sha256 = (file: string) => createHash("sha256").update(readFileSync(file)).digest("hex");

async function ask(page: Page, question: string) {
  const input = page.getByPlaceholder(/질문을 입력하세요/);
  await input.fill(question);
  await input.press("Enter");
  await expect(page.getByRole("log", { name: "대화 내용" }).getByText(question, { exact: true })).toBeVisible();
  await expect(page.getByText(/^\d+ms$/).last()).toBeVisible({ timeout: 15_000 });
}

test("데이터 수명주기: 매일 백업을 만들면 동기화 화면에 최근 백업이 보이고, Agent가 없는 백업만 내려받아 SHA-256을 검증한다", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const stamp = `${Date.now()}`;
  rmSync(BACKUP_DIR, { recursive: true, force: true });

  // 1) 백업이 없으면: "백업 기록 없음"과 하루 넘음 경고
  await page.goto("/sync");
  await expect(page.getByRole("heading", { level: 1, name: "동기화" })).toBeVisible();
  const card = page.getByTestId("last-backup");
  await expect(card).toContainText("백업 기록 없음");
  await expect(page.getByText("마지막 백업이 하루 넘음")).toBeVisible();

  // 2) 매일 03:00 작업과 같은 백업 실행: pg_dump -Fc → age -r → backups/rag-YYYYMMDD.dump.age
  const since = backendLogMark();
  const made = backendCli(["backup"], {
    BACKUP_DIR,
    BACKUP_AGE_RECIPIENT: AGE_RECIPIENT,
    BACKUP_PG_DUMP_CMD: JSON.stringify([process.execPath, path.join(FIXTURES, "fake-pg-dump.mjs")]),
    BACKUP_AGE_CMD: JSON.stringify([process.execPath, path.join(FIXTURES, "fake-age.mjs")]),
  });
  expect(made.status, made.stderr + made.stdout).toBe(0);
  const name = /백업 완료: (rag-\d{8}\.dump\.age)/.exec(made.stdout)?.[1];
  expect(name).toBeTruthy();
  const created = await waitForBackendLog((r) => r.event === "backup.created" && r.name === name, { since });
  expect(created.size).toBeGreaterThan(0);
  // 공개키·DB 주소는 로그에 남기지 않는다
  expect(JSON.stringify(created)).not.toContain(AGE_RECIPIENT);
  const backupFile = path.join(BACKUP_DIR, name!);
  expect(readFileSync(backupFile, "utf-8")).toContain("age-encryption.org/v1");
  // 평문 덤프·임시 파일은 남지 않는다
  expect(readdirSync(BACKUP_DIR).sort()).toEqual([name, `${name}.sha256`]);

  // 3) 동기화 화면: 최근 백업이 "방금"으로, 경고가 사라진다
  await page.reload();
  await expect(card).toContainText("방금");
  await expect(card).toContainText(name!);
  await expect(card).not.toContainText("백업 기록 없음");
  await expect(page.getByText("마지막 백업이 하루 넘음")).toHaveCount(0);
  await waitForBackendLog(
    (r) => r.event === "http.request" && r.method === "GET" && r.path === "/api/sync/status" && r.status === 200,
    { since },
  );

  // 4) Agent: 동기화가 끝나면 없는 백업만 내려받고 SHA-256을 검증한다
  const folder = syncPath(`e2e-lifecycle-${stamp}`);
  mkdirSync(folder, { recursive: true });
  const home = path.resolve(import.meta.dirname, ".tmp", `agent-home-lifecycle-${stamp}`);
  const pcBackups = path.join(home, "pc-backup");
  const token = backendCli(["create-token", `e2e-lifecycle-${stamp}`]);
  expect(token.status, token.stderr).toBe(0);
  const lines = token.stdout.trim().split(/\r?\n/);
  const env = {
    RAG_AGENT_HOME: home,
    RAG_AGENT_SERVER: API_URL,
    RAG_AGENT_TOKEN: lines[lines.length - 1].trim(),
    RAG_AGENT_SCAN_ROOTS: folder,
    RAG_AGENT_BACKUP_DIR: pcBackups,
  };
  const agent = (...args: string[]) =>
    spawnSync("uv", ["run", "rag-agent", ...args], {
      cwd: AGENT_DIR,
      encoding: "utf-8",
      env: { ...process.env, PYTHONIOENCODING: "utf-8", ...env },
    });

  try {
    const since2 = backendLogMark();
    const first = agent("sync");
    expect(first.status, first.stderr + first.stdout).toBe(0);
    expect(first.stdout).toContain("동기화 완료");
    expect(first.stdout).toContain("백업: 내려받음 1, 이미 있음 0, 검증 실패 0");
    const runId = Number(/run_id=(\d+)/.exec(first.stdout)?.[1]);
    // 백엔드: 실행이 끝난 뒤 목록 조회 → 파일 내려주기
    const completed = await waitForBackendLog((r) => r.event === "sync.run.completed" && r.run_id === runId, {
      since: since2,
    });
    const listed = await waitForBackendLog((r) => r.event === "backup.listed", { since: since2 });
    expect(listed.count).toBe(1);
    const downloaded = await waitForBackendLog((r) => r.event === "backup.downloaded" && r.name === name, {
      since: since2,
    });
    expect(downloaded.size).toBe(created.size);
    expect(Date.parse(String(downloaded.timestamp))).toBeGreaterThanOrEqual(Date.parse(String(completed.timestamp)));

    // PC 폴더에 같은 내용(SHA-256이 서버 목록과 같음), 받는 중 파일은 남지 않는다
    const local = path.join(pcBackups, name!);
    expect(existsSync(local)).toBe(true);
    const serverSha = readFileSync(`${backupFile}.sha256`, "utf-8").split(/\s+/)[0];
    expect(sha256(local)).toBe(serverSha);
    expect(readdirSync(pcBackups)).toEqual([name]);
    const agentLog = readFileSync(path.join(home, "agent.log"), "utf-8");
    expect(agentLog).toContain('"event": "agent.backup.downloaded"');
    expect(agentLog).not.toContain(env.RAG_AGENT_TOKEN);

    // 다시 실행하면 이미 있는 백업은 받지 않는다
    const since3 = backendLogMark();
    const second = agent("sync");
    expect(second.status, second.stderr + second.stdout).toBe(0);
    expect(second.stdout).toContain("백업: 내려받음 0, 이미 있음 1");
    await waitForBackendLog((r) => r.event === "backup.listed", { since: since3 });
    expect(findBackendLogs((r) => r.event === "backup.downloaded", since3)).toHaveLength(0);
  } finally {
    rmSync(folder, { recursive: true, force: true });
    rmSync(home, { recursive: true, force: true });
  }
});

test("데이터 수명주기: 로컬 삭제 후 30일이 지난 문서는 매일 영구 삭제되고, 예전 대화의 인용은 그대로 남는다", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const code = codeName(Date.now());
  const file = `회의-${code}.md`;
  rmSync(ROOT, { recursive: true, force: true });
  const folder = path.join(ROOT, `docs-${code}`);
  mkdirSync(folder, { recursive: true });
  writeFileSync(path.join(folder, file), `# ${code} 회의\n\n${code} 회의 결론은 출시를 다음 달로 미루는 것이다.\n`);
  const ingested = backendCli(["ingest-local", folder], {
    SYNC_ALLOWED_ROOT: ROOT,
    UPLOAD_TMP_DIR,
    EMBED_DAILY_FILE_LIMIT: "100000",
  });
  expect(ingested.status, ingested.stderr).toBe(0);

  // 1) 이 문서를 출처로 답한 대화를 남긴다
  const question = `${code} 회의 결론 알려줘`;
  await page.goto("/");
  await ask(page, question);
  await expect(page.getByLabel("출처", { exact: true })).toContainText(file);
  await expect(page).toHaveURL(/\/c\/\d+$/);
  const conversationUrl = page.url();

  // 2) 로컬에서 지워진 지 31일이 지났다: 문서 화면 "로컬 삭제" 탭에 보인다
  markLocallyDeleted(file, 31);
  await page.goto(`/documents?q=${code}&tab=locally_deleted`);
  const row = page.getByRole("list", { name: "문서 목록" }).getByRole("listitem", { name: file });
  await expect(row).toBeVisible();

  // 3) 매일 04:00 작업과 같은 보관 기간 정리
  const since = backendLogMark();
  const purged = backendCli(["purge-retention"]);
  expect(purged.status, purged.stderr).toBe(0);
  expect(purged.stdout).toMatch(/보관 기간\(30일\)이 지난 로컬 삭제 문서 [1-9]\d*개를 영구 삭제했습니다/);
  const event = await waitForBackendLog((r) => r.event === "documents.purged" && r.reason === "retention", {
    since,
  });
  expect(event.documents).toBeGreaterThanOrEqual(1);
  expect(event.masked_messages).toBe(0);
  expect(JSON.stringify(event)).not.toContain(code);

  // 4) 문서 화면에서 사라진다
  await page.reload();
  await expect(row).toHaveCount(0);
  await expect(page.getByText("해당하는 문서가 없습니다.")).toBeVisible();

  // 5) 예전 대화의 인용 구절은 가리지 않는다(프라이버시 삭제가 아님)
  await page.goto(conversationUrl);
  const item = page.getByLabel("출처", { exact: true }).getByRole("listitem").filter({ hasText: file }).first();
  await item.getByRole("button").first().click();
  await expect(item.getByLabel("인용 구절")).toContainText("출시를 다음 달로");
  await expect(item.getByLabel("인용 구절")).not.toHaveText("삭제된 문서");
  await expect(item).not.toContainText("영구 삭제된 문서입니다");
  await waitForBackendLog(
    (r) => r.event === "http.request" && r.method === "GET" && String(r.path).startsWith("/api/conversations/"),
    { since },
  );
});
