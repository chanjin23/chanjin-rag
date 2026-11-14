import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { backendLogMark, findBackendLogs, waitForBackendLog } from "./backendLog";
import { API_URL, BACKEND_LOG, syncPath, TEST_DATABASE_URL, UPLOAD_TMP_DIR } from "./env";

/**
 * T08 Windows Sync Agent: 실제 `rag-agent` CLI를 E2E 백엔드(:8001)에 실행한다.
 * 동기화 화면(T13)은 아직 없어서 화면 대신 CLI 출력, Agent 로그(agent.log), 백엔드 로그로 확인한다.
 *
 * - 서버 경로 검사(허용 폴더 env.ts `SYNC_ROOT`, Windows는 C:\Chanjin\dev) 때문에 실제 파일을
 *   `<SYNC_ROOT>/e2e-agent-<stamp>`에 합성 텍스트로 만들고 끝나면 지운다.
 * - `RAG_AGENT_SCAN_ROOTS`로 서버 루트(SYNC_ROOT)를 이 폴더로 좁힌다. Agent는 스캔하지 않은 서버
 *   루트를 `scan_errors`로 보내므로 다른 문서는 삭제로 판정되지 않는다(실제 문서 폴더는 읽지 않음).
 * - 상태 폴더(`RAG_AGENT_HOME`)는 e2e/.tmp 아래 새 폴더라 실제 Agent의 state.db·뮤텍스와 겹치지 않는다.
 * - `reveal`의 성공 경로는 탐색기 창을 띄우므로 pytest(가짜 실행기)에서만 확인하고, 여기서는 거절만 본다.
 */
const REPO_DIR = path.resolve(import.meta.dirname, "..", "..");
const BACKEND_DIR = path.join(REPO_DIR, "backend");
const AGENT_DIR = path.join(REPO_DIR, "agent");
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

function issueToken(): string {
  const res = backendCli(["create-token", `e2e-agent-${Date.now()}`]);
  expect(res.status, res.stderr).toBe(0);
  const lines = res.stdout.trim().split(/\r?\n/);
  return lines[lines.length - 1].trim();
}

/** 업로드로 쌓인 작업을 Worker와 같은 실행기로 처리해 큐를 비운다(다른 테스트와 간섭 방지). */
function drainJobs() {
  mkdirSync(DRAIN_ROOT, { recursive: true });
  return backendCli(["ingest-local", DRAIN_ROOT], {
    SYNC_ALLOWED_ROOT: DRAIN_ROOT,
    UPLOAD_TMP_DIR,
    LOG_FORMAT: "json",
    LOG_FILE: BACKEND_LOG,
  });
}

type AgentLog = { event: string } & Record<string, unknown>;

function readAgentLog(home: string): AgentLog[] {
  const file = path.join(home, "agent.log");
  if (!existsSync(file)) return [];
  return readFileSync(file, "utf-8")
    .split(/\r?\n/)
    .filter((line) => line.startsWith("{"))
    .map((line) => JSON.parse(line) as AgentLog);
}

test("Sync Agent: rag-agent sync가 폴더를 스캔해 판정대로 업로드하고, chanjin-sync 링크는 로컬 설정으로만 동작한다", async () => {
  test.setTimeout(180_000);
  const stamp = `${Date.now()}`;
  const folder = syncPath(`e2e-agent-${stamp}`);
  const home = path.resolve(import.meta.dirname, ".tmp", `agent-home-${stamp}`);
  const token = issueToken();
  const env = {
    RAG_AGENT_HOME: home,
    RAG_AGENT_SERVER: API_URL,
    RAG_AGENT_TOKEN: token,
    RAG_AGENT_SCAN_ROOTS: folder,
  };
  const agent = (...args: string[]) =>
    spawnSync("uv", ["run", "rag-agent", ...args], {
      cwd: AGENT_DIR,
      encoding: "utf-8",
      env: { ...process.env, PYTHONIOENCODING: "utf-8", ...env },
    });

  mkdirSync(path.join(folder, "회의"), { recursive: true });
  writeFileSync(path.join(folder, "회의", "주간회의.md"), `# 에이전트 ${stamp}\n\n배포는 금요일.\n`);
  writeFileSync(path.join(folder, "메모.TXT"), `에이전트 메모 ${stamp}\n`);
  writeFileSync(path.join(folder, "예전문서.doc"), "old format");
  writeFileSync(path.join(folder, "~$주간회의.md"), "office lock file");

  try {
    // 1) 수동 동기화(F-03): config → runs → manifest → 업로드 → complete
    const since = backendLogMark();
    const first = agent("sync");
    expect(first.status, first.stderr + first.stdout).toBe(0);
    const runId = Number(/run_id=(\d+)/.exec(first.stdout)?.[1]);
    expect(runId).toBeGreaterThan(0);
    expect(first.stdout).toContain("파일 2개, 업로드 2");
    expect(first.stdout).toContain("건너뛴 형식: .doc 1");

    const started = await waitForBackendLog((r) => r.event === "sync.run.started" && r.run_id === runId, { since });
    expect(started.source).toBe("cli");
    const judged = await waitForBackendLog((r) => r.event === "sync.manifest.judged" && r.run_id === runId, { since });
    // 스캔하지 않은 서버 루트 1개를 scan_errors로 보내 삭제 판정을 보류시킨다
    expect(judged).toMatchObject({ files: 2, upload: 2, scan_errors: 1, deleted: 0 });
    const completed = await waitForBackendLog((r) => r.event === "sync.run.completed" && r.run_id === runId, {
      since,
    });
    expect(completed.status).toBe("succeeded");
    const uploads = findBackendLogs((r) => r.event === "sync.file.uploaded" && r.run_id === runId, since);
    expect(uploads.map((u) => u.outcome)).toEqual(["queued", "queued"]);

    // Agent 로그: 이벤트는 남기되 토큰·경로·본문은 남기지 않는다
    const agentLog = readAgentLog(home);
    expect(agentLog.map((r) => r.event)).toEqual(
      expect.arrayContaining(["agent.sync.started", "agent.scan.completed", "agent.manifest.judged", "agent.sync.completed"]),
    );
    const rawLog = readFileSync(path.join(home, "agent.log"), "utf-8");
    expect(rawLog).not.toContain(token);
    expect(rawLog).not.toContain("e2e-agent-");
    expect(rawLog).not.toContain("배포는");

    // 2) 다시 실행하면 해시 캐시를 재사용하고 서버도 바뀐 것이 없다고 판정한다
    const since2 = backendLogMark();
    const second = agent("sync");
    expect(second.status, second.stderr + second.stdout).toBe(0);
    expect(second.stdout).toContain("업로드 0");
    expect(second.stdout).toContain("해시 재사용 2");
    const run2 = Number(/run_id=(\d+)/.exec(second.stdout)?.[1]);
    const judged2 = await waitForBackendLog((r) => r.event === "sync.manifest.judged" && r.run_id === run2, {
      since: since2,
    });
    expect(judged2).toMatchObject({ files: 2, upload: 0 });

    const status = agent("status");
    expect(status.status).toBe(0);
    expect(status.stdout).toContain(`마지막 실행: succeeded (run_id=${run2}, 출처 cli`);
    expect(status.stdout).toContain("재시도 큐: 0개");
    expect(status.stdout).not.toContain(token);

    // 3) chanjin-sync://run: URL의 서버 주소는 무시하고 로컬 설정의 서버로 source=web 실행
    const since3 = backendLogMark();
    const viaLink = agent("protocol", "chanjin-sync://run?source=web&server=http://127.0.0.1:9");
    expect(viaLink.status, viaLink.stderr).toBe(0);
    const webRun = await waitForBackendLog((r) => r.event === "sync.run.started" && r.source === "web", {
      since: since3,
    });
    await waitForBackendLog((r) => r.event === "sync.run.completed" && r.run_id === webRun.run_id, { since: since3 });

    // 1분 안의 두 번째 run, 형식이 틀린 reveal은 아무것도 하지 않고 agent.log에만 남긴다
    const since4 = backendLogMark();
    expect(agent("protocol", "chanjin-sync://run?source=web").status).toBe(0);
    expect(agent("protocol", "chanjin-sync://reveal?k=..%5C..%5CWindows%5Cnotepad.exe").status).toBe(0);
    const protocolLog = readAgentLog(home).filter((r) => r.event.startsWith("agent.protocol."));
    expect(protocolLog.map((r) => [r.event, r.reason ?? null])).toEqual([
      ["agent.protocol.ran", null],
      ["agent.protocol.rate_limited", null],
      ["agent.protocol.rejected", "invalid_key"],
    ]);
    expect(findBackendLogs((r) => r.event === "sync.run.started", since4)).toEqual([]);
  } finally {
    rmSync(folder, { recursive: true, force: true });
    rmSync(home, { recursive: true, force: true });
  }

  const drained = drainJobs();
  expect(drained.status, drained.stderr).toBe(0);
});
