import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { backendLogMark, waitForBackendLog } from "./backendLog";
import { BACKEND_LOG, TEST_DATABASE_URL, UPLOAD_TMP_DIR } from "./env";

/**
 * 첫 실행 체크리스트와 전역 배너 (F-18, TRD 8.3 `/setup/status`·`/health`, 9.1, 12.4).
 * - 체크리스트: 테스트 DB의 문서를 모두 지워 "문서 0개" 상태를 실제로 만든다. 다른 spec은 자기 문서를
 *   beforeAll에서 새로 만들므로 영향이 없다.
 * - 배너: 합성 문서를 `ingest-local`로 넣고, 질문에 `[[fake-error-once:provider_auth]]`를 붙여 Fake LLM이
 *   인증 오류를 내게 한다. 채팅 API가 provider_status에 실패를 기록 → `/health`의 `provider_auth` 이상 →
 *   모든 화면 위 배너. 마지막에 정상 질문으로 복구(성공 기록)해 다음 spec에 배너가 남지 않게 한다.
 */
const BACKEND_DIR = path.resolve(import.meta.dirname, "..", "..", "backend");
const ROOT = path.resolve(import.meta.dirname, ".tmp", "setup-root");
const stamp = Date.now();
/** 실행마다 다른 영문 코드명(Kiwi가 한 토큰으로 자르는 알파벳만) */
const CODE = `zs${stamp.toString(26).replace(/[0-9]/g, (d) => String.fromCharCode(103 + Number(d)))}`;

const BACKEND_ENV = {
  ...process.env,
  APP_ENV: "test",
  PROVIDER: "fake",
  DATABASE_URL: TEST_DATABASE_URL,
  LOG_FORMAT: "json",
  LOG_FILE: BACKEND_LOG,
  SYNC_ALLOWED_ROOT: ROOT,
  UPLOAD_TMP_DIR,
  PYTHONIOENCODING: "utf-8",
};

function runPython(code: string) {
  const res = spawnSync("uv", ["run", "python", "-c", code], {
    cwd: BACKEND_DIR,
    encoding: "utf-8",
    env: BACKEND_ENV,
  });
  expect(res.status, res.stderr).toBe(0);
  return res.stdout;
}

/** 문서를 모두 지운다(sections·chunks·jobs는 외래 키 CASCADE). */
function deleteAllDocuments(): void {
  runPython(
    [
      "import os, psycopg",
      "url = os.environ['DATABASE_URL'].replace('+psycopg', '')",
      "with psycopg.connect(url) as conn:",
      "    conn.execute('DELETE FROM documents')",
    ].join("\n"),
  );
}

const log = (page: Page) => page.getByRole("log", { name: "대화 내용" });

async function ask(page: Page, question: string) {
  const input = page.getByPlaceholder(/질문을 입력하세요/);
  await input.fill(question);
  await input.press("Enter");
  await expect(log(page).getByText(question, { exact: true })).toBeVisible();
}

test.describe("첫 실행 체크리스트와 전역 배너 (F-18)", () => {
  test("문서가 없으면 첫 실행 체크리스트가 보인다", async ({ page }) => {
    deleteAllDocuments();
    const since = backendLogMark();
    await page.goto("/");

    const checklist = page.getByTestId("setup-checklist");
    await expect(checklist.getByRole("heading", { name: "첫 실행 체크리스트" })).toBeVisible();
    await expect(checklist).toContainText("아직 수집된 문서가 없습니다");

    // 백엔드: 문서 0개로 점검했다는 로그와, 화면의 완료 표시가 API 응답과 같은지
    const checked = await waitForBackendLog(
      (r) => r.event === "setup.status.checked" && r.documents === 0,
      { since },
    );
    const res = await page.request.get("/api/setup/status");
    expect(res.ok()).toBe(true);
    const status = (await res.json()) as {
      documents: number;
      completed: number;
      total: number;
      steps: { key: string; title: string; command: string | null; done: boolean }[];
    };
    expect(status.documents).toBe(0);
    expect(checked.completed).toBe(status.completed);
    expect(status.steps.map((s) => s.key)).toEqual(["user", "token", "agent", "first_sync", "documents"]);
    await expect(page.getByTestId("setup-progress")).toHaveText(`(${status.completed}/${status.total} 완료)`);

    for (const step of status.steps) {
      const item = page.getByTestId(`setup-step-${step.key}`);
      await expect(item).toContainText(step.title);
      await expect(item).toHaveAttribute("data-done", String(step.done));
      await expect(item).toContainText(step.done ? "완료" : "할 일");
      if (step.command) await expect(item.locator("code")).toHaveText(step.command);
    }
    // E2E 사용자가 있으므로 계정 단계는 완료, 문서 단계는 할 일
    await expect(page.getByTestId("setup-step-user")).toHaveAttribute("data-done", "true");
    await expect(page.getByTestId("setup-step-documents")).toHaveAttribute("data-done", "false");
    await expect(page.getByTestId("setup-step-token").locator("code")).toContainText("rag-backend create-token");
    await expect(page.getByTestId("setup-step-first_sync").locator("code")).toHaveText("rag-agent sync");

    // 질문하면 문서가 없다는 답과 체크리스트 안내(TRD 7.1)
    const question = `${CODE} 문서 없을 때 질문`;
    const askedAt = backendLogMark();
    await ask(page, question);
    await expect(log(page).getByTestId("assistant-message").last()).toContainText(
      "아직 색인된 문서가 없습니다. 첫 실행 체크리스트에서",
      { timeout: 10_000 },
    );
    await waitForBackendLog((r) => r.event === "chat.message.received" && r.text === question, {
      since: askedAt,
    });
  });

  test("Provider 인증 오류가 나면 전역 배너가 보인다", async ({ page }) => {
    test.setTimeout(120_000);
    // 질문이 모델까지 가려면 문서가 있어야 한다(문서 0개면 모델을 부르지 않음)
    rmSync(ROOT, { recursive: true, force: true });
    const folder = path.join(ROOT, `docs-${stamp}`);
    mkdirSync(folder, { recursive: true });
    writeFileSync(path.join(folder, `배너-${CODE}.md`), `# ${CODE} 일정\n\n${CODE} 일정의 마감일은 12월 20일이다.\n`);
    const ingest = spawnSync("uv", ["run", "rag-backend", "ingest-local", folder], {
      cwd: BACKEND_DIR,
      encoding: "utf-8",
      env: BACKEND_ENV,
    });
    expect(ingest.status, ingest.stderr).toBe(0);

    const banner = page.getByRole("alert", { name: "모델 API 인증 오류" });
    const since = backendLogMark();
    await page.goto("/");
    await expect(page.getByTestId("setup-checklist")).toHaveCount(0);
    await expect(banner).toHaveCount(0);

    // 1) 인증 오류 → 답변 말풍선 오류 + 모든 화면 위 배너
    await ask(page, `${CODE} 마감일 [[fake-error-once:provider_auth]]`);
    await expect(log(page).getByTestId("chat-error")).toContainText("API 키 또는 사용량을 확인하세요", {
      timeout: 10_000,
    });
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("API 키 또는 사용량을 확인하세요");
    await waitForBackendLog(
      (r) => r.event === "provider.failure.recorded" && r.kind === "llm" && r.error_code === "provider_auth",
      { since },
    );
    const degraded = await waitForBackendLog(
      (r) =>
        r.event === "health.degraded" &&
        Array.isArray(r.issues) &&
        (r.issues as string[]).includes("provider_auth"),
      { since },
    );
    expect(degraded.providers).toMatchObject({ llm: "provider_auth" });

    // 다른 화면에서도 보인다
    await page.getByRole("navigation").getByRole("link", { name: "문서" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "문서" })).toBeVisible();
    await expect(banner).toBeVisible();

    // 2) 닫으면 숨고, 같은 상태에서는 새로 고쳐도 숨은 채로 있다(닫음은 sessionStorage)
    await banner.getByRole("button", { name: "모델 API 인증 오류 알림 닫기" }).click();
    await expect(banner).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole("heading", { level: 1, name: "문서" })).toBeVisible();
    await expect(banner).toHaveCount(0);

    // 3) 같은 장애가 이어지는 동안(연속 실패)은 다시 뜨지 않는다(since = 연속 실패의 첫 시각)
    await page.getByRole("navigation").getByRole("link", { name: "채팅" }).click();
    const repeat = backendLogMark();
    await ask(page, `${CODE} 담당자 [[fake-error-once:provider_auth]]`);
    await expect(log(page).getByTestId("chat-error")).toContainText("API 키 또는 사용량을 확인하세요", {
      timeout: 10_000,
    });
    await waitForBackendLog(
      (r) => r.event === "provider.failure.recorded" && r.error_code === "provider_auth",
      { since: repeat },
    );
    const details = await (await page.request.get("/api/health/details")).json();
    expect(details.issues.map((i: { code: string }) => i.code)).toContain("provider_auth");
    await page.reload();
    await expect(page.getByPlaceholder(/질문을 입력하세요/)).toBeVisible();
    await expect(banner).toHaveCount(0);

    // 4) 모델 호출이 다시 성공하면 배너 원인이 사라진다
    const healed = backendLogMark();
    await ask(page, `${CODE} 일정 마감일`);
    await expect(log(page).getByTestId("assistant-message").last()).toContainText("12월 20일", {
      timeout: 10_000,
    });
    await waitForBackendLog((r) => r.event === "chat.message.completed", { since: healed });
    await expect
      .poll(async () => {
        const h = await (await page.request.get("/api/health/details")).json();
        return h.issues.map((i: { code: string }) => i.code).includes("provider_auth");
      })
      .toBe(false);

    // 5) 다시 인증 오류가 나면(새 장애) 닫았던 배너가 다시 보인다
    await page.reload();
    await expect(page.getByPlaceholder(/질문을 입력하세요/)).toBeVisible();
    const relapse = backendLogMark();
    await ask(page, `${CODE} 장소 [[fake-error-once:provider_auth]]`);
    await expect(banner).toBeVisible({ timeout: 10_000 });
    await waitForBackendLog(
      (r) => r.event === "health.degraded" && (r.issues as string[]).includes("provider_auth"),
      { since: relapse },
    );

    // 6) 정상 질문으로 복구해 다음 spec에 배너를 남기지 않는다
    const cleanup = backendLogMark();
    await ask(page, `${CODE} 일정 마감일 다시`);
    await waitForBackendLog((r) => r.event === "chat.message.completed" || r.event === "chat.message.not_found", {
      since: cleanup,
      timeoutMs: 10_000,
    });
    await expect(banner).toHaveCount(0, { timeout: 10_000 });
    const health = await (await page.request.get("/api/health")).json();
    expect(health.issues).not.toContainEqual({ code: "provider_auth" });
  });

  test("Worker 하트비트가 끊기면 전역 배너가 보인다", async ({ page }) => {
    // 2분 전 하트비트(90초 넘음)를 넣어 끊긴 상태를 만들고, 끝나면 지운다
    const setHeartbeat = (sql: string) =>
      runPython(
        [
          "import os, psycopg",
          "url = os.environ['DATABASE_URL'].replace('+psycopg', '')",
          "with psycopg.connect(url) as conn:",
          `    conn.execute("""${sql}""")`,
        ].join("\n"),
      );
    setHeartbeat(
      "INSERT INTO worker_heartbeats (name, started_at, heartbeat_at, pid) VALUES " +
        "('e2e-worker', now() - interval '1 hour', now() - interval '2 minutes', 1) " +
        "ON CONFLICT (name) DO UPDATE SET heartbeat_at = now() - interval '2 minutes'",
    );
    try {
      const since = backendLogMark();
      await page.goto("/sync");
      const banner = page.getByRole("alert", { name: "Worker 응답 없음" });
      await expect(banner).toBeVisible();
      await expect(banner).toContainText("Worker 하트비트가 끊겼습니다");
      await expect(banner).toContainText(/분 전/);
      await waitForBackendLog(
        (r) => r.event === "health.degraded" && (r.issues as string[]).includes("worker_stale"),
        { since },
      );
      // 닫으면 숨는다
      await banner.getByRole("button", { name: "Worker 응답 없음 알림 닫기" }).click();
      await expect(banner).toHaveCount(0);
    } finally {
      setHeartbeat("DELETE FROM worker_heartbeats WHERE name = 'e2e-worker'");
    }
  });
});
