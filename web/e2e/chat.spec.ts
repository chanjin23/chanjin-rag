import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { backendLogMark, findBackendLogs, waitForBackendLog } from "./backendLog";
import { BACKEND_LOG, TEST_DATABASE_URL, UPLOAD_TMP_DIR } from "./env";

/**
 * T10 질의 그래프와 SSE (F-08, TRD 7.1, 7.3).
 * 시드: `rag-backend ingest-local`로 실제 수집 경로(파싱 → 청킹 → Kiwi → Fake 임베딩)를 거친 문서를 넣는다.
 * 모델은 Fake Provider(그래프 프롬프트에 결정적으로 답하는 응답기)라, 문서에 있는 질문은 출처가 붙은
 * 답변을, 없는 질문은 재검색 후 "찾지 못함"을 낸다. 테스트 DB는 E2E 실행 사이에 남으므로 실행마다
 * 다른 고유 낱말(코드명)을 문서와 질문에 넣어, 이번 실행의 문서만 확실히 맞게 한다.
 * 백엔드는 FAKE_LLM_TOKEN_DELAY_MS로 조각 사이를 늦춰 단계 표시를 화면에서 확인할 수 있다.
 */
const ROOT = path.resolve(import.meta.dirname, ".tmp", "chat-root");

/** 실행마다 다른 영문 코드명(Kiwi가 한 토큰으로 자르는 알파벳만). */
function codeName(seed: number): string {
  let n = seed;
  let out = "";
  while (n > 0) {
    out += String.fromCharCode(97 + (n % 26));
    n = Math.floor(n / 26);
  }
  return `zq${out}`;
}

const stamp = Date.now();
const CODE = codeName(stamp);
const FILE = `프로젝트-${CODE}.md`;

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
      UPLOAD_TMP_DIR,
      PYTHONIOENCODING: "utf-8",
    },
  });
}

test.describe("채팅 (F-08 질의 그래프와 SSE)", () => {
  test.beforeAll(() => {
    test.setTimeout(120_000);
    rmSync(ROOT, { recursive: true, force: true });
    const folder = path.join(ROOT, `docs-${stamp}`);
    mkdirSync(folder, { recursive: true });
    writeFileSync(
      path.join(folder, FILE),
      `# ${CODE} 프로젝트\n\n${CODE} 프로젝트의 마감일은 12월 15일이다.\n\n${CODE} 프로젝트 담당자는 기획팀이다.\n`,
    );
    const res = ingestLocal(folder);
    expect(res.status, res.stderr).toBe(0);
    expect(res.stdout).toContain("접수: 새 버전 1");
  });

  test("질문하면 단계 표시 후 출처가 붙은 답변이 스트리밍된다", async ({ page }) => {
    const question = `${CODE} 프로젝트 마감일 알려줘`;
    const since = backendLogMark();

    await page.goto("/");
    const input = page.getByPlaceholder(/질문을 입력하세요/);
    await input.fill(question);
    await input.press("Enter");

    // 1) 화면: 내 질문, 진행 단계, 출처가 붙은 답변(스트리밍), 끝나면 단계 표시가 사라짐
    await expect(page.getByText(question, { exact: true })).toBeVisible();
    await expect(input).toHaveValue("");
    await expect(page.getByRole("status")).toContainText("답변 작성 중");
    await expect(page.getByRole("button", { name: "중지" })).toBeVisible();
    const answer = page.getByText(/자료에 따르면 .*12월 15일이다\. \[\d\]/);
    await expect(answer).toBeVisible();
    const sources = page.getByLabel("출처");
    await expect(sources).toContainText(FILE);
    await expect(sources).toContainText(/\[\d\]/);
    await expect(page.getByText(/^\d+ms$/)).toBeVisible();
    await expect(page.getByRole("status")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "전송" })).toBeVisible();

    // 인용 번호는 실제 출처 목록에 있는 번호다
    const cited = (await answer.textContent())?.match(/\[(\d)\]/)?.[1];
    await expect(sources.getByRole("listitem").filter({ hasText: `[${cited}]` })).toContainText(FILE);

    // 2) 백엔드: 요청 수신 → 그래프 완료(찾음), 같은 메시지 ID
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
      { since },
    );
    expect(received.length).toBe(question.length);
    expect(typeof received.conversation_id).toBe("number");
    const completed = await waitForBackendLog(
      (r) => r.event === "chat.message.completed" && r.message_id === received.message_id,
      { since },
    );
    expect(completed.not_found).toBe(false);
    expect(completed.grade).toBe("sufficient");
    expect(completed.cited).toBeGreaterThan(0);
    await waitForBackendLog(
      (r) =>
        r.event === "http.request" &&
        r.method === "POST" &&
        r.path === "/api/conversations/new/messages" &&
        r.status === 200,
      { since },
    );
    // 운영 기준: 완료 로그에 질문·답변 원문이 없다
    expect(JSON.stringify(completed)).not.toContain(CODE);
  });

  test("문서에 없는 질문은 찾지 못함과 검색 범위를 보여준다", async ({ page }) => {
    const question = `${codeName(stamp + 7)} 화성 기지의 산소 공급량은?`;
    const since = backendLogMark();

    await page.goto("/");
    const input = page.getByPlaceholder(/질문을 입력하세요/);
    await input.fill(question);
    await input.press("Enter");

    // 1) 화면: 다시 검색하는 단계를 거쳐 찾지 못함 문구, 검색 범위, 참고 후보
    await expect(page.getByText(question, { exact: true })).toBeVisible();
    await expect(page.getByRole("status")).toContainText(/근거 확인 중|다시 검색 중|문서 검색 중/);
    await expect(page.getByText("문서에서 찾지 못했습니다.")).toBeVisible();
    const coverage = page.getByTestId("coverage");
    await expect(coverage).toContainText(/검색 범위: 키워드 검색 \d+개 · 의미 검색 \d+개 · 대기·처리 중 \d+개 문서/);
    await expect(page.getByLabel("참고 후보").getByRole("listitem")).not.toHaveCount(0);
    await expect(page.getByLabel("출처")).toHaveCount(0);

    // 2) 백엔드: 수신 → 재검색 1회 후 찾지 못함, 검색 범위가 화면과 같다
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
      { since },
    );
    const notFound = await waitForBackendLog(
      (r) => r.event === "chat.message.not_found" && r.message_id === received.message_id,
      { since },
    );
    expect(notFound.not_found).toBe(true);
    expect(notFound.retried).toBe(true);
    const cov = notFound.coverage as { bm25_docs: number; vector_docs: number; total_docs: number };
    expect(cov.total_docs).toBeGreaterThan(0);
    await expect(coverage).toContainText(`키워드 검색 ${cov.bm25_docs}개 · 의미 검색 ${cov.vector_docs}개`);
    expect(findBackendLogs((r) => r.event === "chat.message.completed", since)).toHaveLength(0);
  });

  test("빈 입력은 보내지 않는다", async ({ page }) => {
    const since = backendLogMark();
    await page.goto("/");
    const input = page.getByPlaceholder(/질문을 입력하세요/);
    await input.fill("   ");
    await expect(page.getByRole("button", { name: "전송" })).toBeDisabled();
    await input.press("Enter");

    // 화면: 말풍선이 생기지 않고 안내 문구가 그대로, 입력도 그대로
    await expect(page.getByText(/내 문서에 대해 질문해 보세요/)).toBeVisible();
    await expect(input).toHaveValue("   ");
    // 백엔드: 화면을 여는 요청(로그인 확인)은 처리됐지만 질문 요청은 오지 않았다
    await waitForBackendLog(
      (r) => r.event === "http.request" && r.path === "/api/auth/me" && r.status === 200,
      { since },
    );
    await page.waitForTimeout(500);
    expect(findBackendLogs((r) => r.event === "chat.message.received", since)).toHaveLength(0);
    expect(
      findBackendLogs((r) => r.event === "http.request" && String(r.path).endsWith("/messages"), since),
    ).toHaveLength(0);
  });
});
