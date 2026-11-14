import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { backendLogMark, findBackendLogs, waitForBackendLog } from "./backendLog";
import { BACKEND_LOG, TEST_DATABASE_URL, UPLOAD_TMP_DIR, WEB_URL } from "./env";

/**
 * 채팅 화면: T10 질의 그래프와 SSE (F-08, TRD 7.1, 7.3), T11 대화 관리·출처 패널·인용·피드백
 * (F-10, F-11, F-12, F-18, TRD 9.1, 9.2).
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
/** 외부 이미지·링크·원시 HTML이 든 문서(Fake 답변이 그 문장을 그대로 옮긴다) */
const LEAK_CODE = codeName(stamp + 13);
const LEAK_FILE = `보안-${LEAK_CODE}.md`;
const LEAK_HOST = "leak.invalid";
/** 답변 화면 질문은 사이드바 대화 제목과 글자가 같을 수 있어 메시지 영역 안에서 찾는다 */
const log = (page: Page) => page.getByRole("log", { name: "대화 내용" });

async function ask(page: Page, question: string) {
  const input = page.getByPlaceholder(/질문을 입력하세요/);
  await input.fill(question);
  await input.press("Enter");
  await expect(log(page).getByText(question, { exact: true })).toBeVisible();
}

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
    writeFileSync(
      path.join(folder, LEAK_FILE),
      `# ${LEAK_CODE} 보안 점검\n\n` +
        `${LEAK_CODE} 보안 점검 결과는 ![유출 그림](http://${LEAK_HOST}/img.png?d=secret) 그림과 ` +
        `[유출 링크](http://${LEAK_HOST}/page) 링크, <img src="http://${LEAK_HOST}/raw.png"> 원시 태그, ` +
        "`[1]` 코드에 정리했다\n",
    );
    const res = ingestLocal(folder);
    expect(res.status, res.stderr).toBe(0);
    expect(res.stdout).toContain("접수: 새 버전 2");
  });

  test("질문하면 단계 표시 후 출처가 붙은 답변이 스트리밍된다", async ({ page }) => {
    const question = `${CODE} 프로젝트 마감일 알려줘`;
    const since = backendLogMark();

    await page.goto("/");
    const input = page.getByPlaceholder(/질문을 입력하세요/);
    await input.fill(question);
    await input.press("Enter");

    // 1) 화면: 내 질문, 진행 단계, 출처가 붙은 답변(스트리밍), 끝나면 단계 표시가 사라짐
    await expect(log(page).getByText(question, { exact: true })).toBeVisible();
    await expect(input).toHaveValue("");
    await expect(page.getByRole("status")).toContainText("답변 작성 중");
    await expect(page.getByRole("button", { name: "중지" })).toBeVisible();
    const answer = page.getByText(/자료에 따르면 .*12월 15일이다\. \[\d\]/);
    await expect(answer).toBeVisible();
    const sources = page.getByLabel("출처", { exact: true });
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
    await expect(log(page).getByText(question, { exact: true })).toBeVisible();
    await expect(page.getByRole("status")).toContainText(/근거 확인 중|다시 검색 중|문서 검색 중/);
    await expect(page.getByText("문서에서 찾지 못했습니다.")).toBeVisible();
    const coverage = page.getByTestId("coverage");
    await expect(coverage).toContainText(/검색 범위: 키워드 검색 \d+개 · 의미 검색 \d+개 · 대기·처리 중 \d+개 문서/);
    await expect(page.getByLabel("참고 후보").getByRole("listitem")).not.toHaveCount(0);
    await expect(page.getByLabel("출처", { exact: true })).toHaveCount(0);

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

  test("인용 칩을 누르면 출처 패널의 해당 항목이 열린다", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    // "폴더에서 보기"는 Windows 브라우저에서만 보인다. E2E를 Linux(WSL)에서 돌려도 운영 사용자(Windows)의
    // 화면을 확인하도록 브라우저 플랫폼을 Windows로 둔다.
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "userAgentData", {
        configurable: true,
        get: () => ({ platform: "Windows" }),
      });
    });
    const question = `${CODE} 프로젝트 마감일 알려줘 (인용 칩)`;
    const since = backendLogMark();

    await page.goto("/");
    await ask(page, question);

    // 1) 답변의 [n]은 버튼(인용 칩)이다. 처음에는 출처 패널의 항목이 접혀 있다
    const answer = log(page).getByTestId("assistant-message").last();
    const chip = answer.locator("button[data-citation]").first();
    await expect(chip).toBeVisible({ timeout: 10_000 });
    const n = await chip.getAttribute("data-citation");
    await expect(chip).toHaveAccessibleName(`인용 ${n}`);
    await expect(log(page)).toHaveAttribute("aria-live", "polite");
    const panel = page.getByRole("complementary", { name: "출처 패널" });
    const item = panel
      .getByLabel("출처", { exact: true })
      .getByRole("listitem")
      .filter({ hasText: `[${n}]` });
    await expect(item).toContainText(FILE);
    await expect(item.getByRole("button", { expanded: false })).toBeVisible();
    await expect(item.getByTestId("source-detail")).toHaveCount(0);

    // 2) 칩을 누르면 그 항목이 펼쳐져 경로·위치·수정일·인용 구절·문서 상세·폴더에서 보기가 보인다
    await chip.click();
    await expect(item.getByRole("button", { expanded: true })).toBeVisible();
    const detail = item.getByTestId("source-detail");
    await expect(detail).toBeVisible();
    await expect(detail.getByTestId("source-path")).toHaveText(new RegExp(`${FILE}$`));
    await expect(detail).toContainText("수정일");
    await expect(detail).toContainText(/\d+-\d+줄/);
    await expect(detail.getByLabel("인용 구절")).toContainText(CODE);
    await expect(detail.getByRole("link", { name: "문서 상세" })).toHaveAttribute(
      "href",
      /^\/documents\/\d+\?loc=%7B.+%7D$/,
    );
    // Windows에서만 "폴더에서 보기"(E2E는 Windows Chrome). 경로 대신 path_key의 SHA-256만 넘긴다
    await expect(detail.getByRole("link", { name: "폴더에서 보기" })).toHaveAttribute(
      "href",
      /^chanjin-sync:\/\/reveal\?k=[0-9a-f]{64}$/,
    );
    // 로컬 경로는 링크가 아니라 복사 버튼
    await detail.getByRole("button", { name: "경로 복사" }).click();
    await expect(detail.getByRole("button", { name: "복사됨" })).toBeVisible();
    expect(await page.evaluate<string>("navigator.clipboard.readText()")).toMatch(new RegExp(`${FILE}$`));

    // 3) 패널을 접고 다시 칩을 누르면 패널이 열리며 그 항목이 펼쳐져 있다
    await page.getByRole("button", { name: "출처 패널 접기" }).click();
    await expect(panel).toHaveCount(0);
    await chip.click();
    await expect(panel).toBeVisible();
    await expect(item.getByTestId("source-detail")).toBeVisible();

    // 4) 백엔드: 인용이 있는 답변 완료
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
      { since },
    );
    const completed = await waitForBackendLog(
      (r) => r.event === "chat.message.completed" && r.message_id === received.message_id,
      { since },
    );
    expect(completed.cited).toBeGreaterThan(0);
  });

  test("대화 이름을 바꾸고 삭제할 수 있다", async ({ page }) => {
    const question = `${CODE} 프로젝트 담당자는 누구야`;
    const renamed = `이름 바꾼 대화 ${CODE}`;
    const since = backendLogMark();

    await page.goto("/");
    await ask(page, question);
    await expect(page.getByText(/^\d+ms$/)).toBeVisible({ timeout: 10_000 });

    // 1) 첫 질문으로 대화가 생기고 URL이 /c/{id}로 바뀌며, 목록에 질문 앞 40자 제목으로 보인다
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
      { since },
    );
    const id = received.conversation_id as number;
    const created = await waitForBackendLog(
      (r) => r.event === "conversation.created" && r.conversation_id === id,
      { since },
    );
    expect(created.source).toBe("message");
    await expect(page).toHaveURL(`${WEB_URL}/c/${id}`);
    const list = page.getByRole("region", { name: "대화 목록" });
    const entry = list.getByRole("listitem").filter({ has: page.locator(`a[href="/c/${id}"]`) });
    await expect(entry).toContainText(question);
    await expect(entry.getByRole("link")).toHaveAttribute("aria-current", "page");

    // 2) 이름 변경: 입력 후 Enter
    await entry.getByRole("button", { name: `${question} 이름 변경` }).click();
    const nameInput = list.getByLabel("새 대화 이름");
    await expect(nameInput).toHaveValue(question);
    await nameInput.fill(renamed);
    await nameInput.press("Enter");
    const renamedEntry = list.getByRole("listitem").filter({ hasText: renamed });
    await expect(renamedEntry).toBeVisible();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(renamed);
    const renameLog = await waitForBackendLog(
      (r) => r.event === "conversation.renamed" && r.conversation_id === id,
      { since },
    );
    expect(renameLog.title_chars).toBe(renamed.length);
    expect(JSON.stringify(renameLog)).not.toContain(renamed); // 제목 원문은 로그에 없다
    await waitForBackendLog(
      (r) =>
        r.event === "http.request" &&
        r.method === "PATCH" &&
        r.path === `/api/conversations/${id}` &&
        r.status === 200,
      { since },
    );

    // 새로고침해도 바뀐 이름과 메시지가 남아 있다(서버에서 불러옴)
    await page.reload();
    await expect(list.getByRole("listitem").filter({ hasText: renamed })).toBeVisible();
    await expect(log(page).getByText(question, { exact: true })).toBeVisible();
    await expect(log(page).getByText(/자료에 따르면 .*기획팀이다\./)).toBeVisible();

    // 3) 삭제: 확인 후 목록에서 사라지고 새 대화 화면으로 간다
    await renamedEntry.getByRole("button", { name: `${renamed} 삭제` }).click();
    const confirm = list.getByRole("alertdialog", { name: "대화 삭제 확인" });
    await expect(confirm).toContainText("삭제할까요?");
    await confirm.getByRole("button", { name: "삭제" }).click();
    await expect(list.getByText(renamed)).toHaveCount(0);
    await expect(page).toHaveURL(`${WEB_URL}/`);
    await expect(page.getByText(/내 문서에 대해 질문해 보세요/)).toBeVisible();

    // 4) 백엔드: 메시지 2개(질문·답변)와 질의 로그 1개가 함께 지워졌다. 다시 열면 없는 대화다
    const deleted = await waitForBackendLog(
      (r) => r.event === "conversation.deleted" && r.conversation_id === id,
      { since },
    );
    expect(deleted.messages).toBe(2);
    expect(deleted.query_logs).toBe(1);
    const res = await page.request.get(`/api/conversations/${id}`);
    expect(res.status()).toBe(404);
  });

  test("답변의 외부 이미지와 링크는 불러오지 않는다", async ({ page }) => {
    const question = `${LEAK_CODE} 보안 점검 결과는?`;
    const external: string[] = [];
    page.on("request", (req) => {
      if (req.url().includes(LEAK_HOST)) external.push(req.url());
    });
    const since = backendLogMark();

    await page.goto("/");
    await ask(page, question);
    const answer = log(page).getByTestId("assistant-message").last();
    await expect(answer).toContainText(/자료에 따르면 .*정리했다/, { timeout: 10_000 });
    await expect(page.getByText(/^\d+ms$/)).toBeVisible({ timeout: 10_000 });

    // 1) 이미지는 대체 텍스트만, 링크는 글자와 주소를 텍스트로만, 원시 HTML은 그리지 않는다
    await expect(answer.getByText("[이미지: 유출 그림]")).toBeVisible();
    await expect(answer).toContainText(`유출 링크 (http://${LEAK_HOST}/page)`);
    await expect(answer.locator("img")).toHaveCount(0);
    await expect(answer.locator("a")).toHaveCount(0);
    await expect(answer.locator("[src], [href]")).toHaveCount(0);
    // 코드 안의 [1]은 칩으로 바꾸지 않는다: 칩은 답변 끝의 인용 하나뿐
    await expect(answer.locator("code")).toHaveText("[1]");
    await expect(answer.locator("button[data-citation]")).toHaveCount(1);

    // 2) 브라우저가 외부 주소로 요청을 보내지 않았다
    await page.waitForTimeout(300);
    expect(external).toEqual([]);

    // 3) 백엔드: 답변 완료(그 문서를 인용)
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
      { since },
    );
    const completed = await waitForBackendLog(
      (r) => r.event === "chat.message.completed" && r.message_id === received.message_id,
      { since },
    );
    expect(completed.cited).toBe(1);
  });

  test("오류 응답에는 다시 시도 버튼이 보인다", async ({ page }) => {
    // Fake LLM은 이 표식이 있는 질문의 첫 모델 호출에서만 provider_unavailable을 낸다(다시 보내면 성공)
    const question = `${CODE} 프로젝트 마감일 [[fake-error-once:provider_unavailable]]`;
    const since = backendLogMark();

    await page.goto("/");
    await ask(page, question);

    // 1) 오류 말풍선: 화면 문구(TRD 7.3)와 다시 시도 버튼
    const error = log(page).getByTestId("chat-error");
    await expect(error).toContainText("모델 서버에 연결하지 못했습니다", { timeout: 10_000 });
    const retry = error.getByRole("button", { name: "다시 시도" });
    await expect(retry).toBeVisible();
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
      { since },
    );
    await waitForBackendLog(
      (r) =>
        r.event === "chat.message.failed" &&
        r.code === "provider_unavailable" &&
        r.message_id === received.message_id,
      { since },
    );

    // 2) 다시 시도: 같은 질문을 같은 대화로 다시 보내고, 이번에는 답변이 끝난다
    const retryMark = backendLogMark();
    await retry.click();
    await expect(log(page).getByTestId("chat-error")).toHaveCount(0);
    await expect(log(page).getByText(/자료에 따르면 .*12월 15일이다\./)).toBeVisible({
      timeout: 10_000,
    });
    await expect(log(page).getByText(question, { exact: true })).toHaveCount(1);

    // 3) 백엔드: 같은 질문이 같은 대화에 다시 접수되어 완료됐다
    const again = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
      { since: retryMark },
    );
    expect(again.conversation_id).toBe(received.conversation_id);
    await waitForBackendLog(
      (r) => r.event === "chat.message.completed" && r.message_id === again.message_id,
      { since: retryMark },
    );
  });

  test("다시 시도할 수 없는 오류에는 버튼이 없다", async ({ page }) => {
    const question = `${CODE} 마감일 [[fake-error-once:provider_auth]]`;
    const since = backendLogMark();
    await page.goto("/");
    await ask(page, question);
    const error = log(page).getByTestId("chat-error");
    await expect(error).toContainText("API 키 또는 사용량을 확인하세요", { timeout: 10_000 });
    await waitForBackendLog(
      (r) => r.event === "chat.message.failed" && r.code === "provider_auth",
      { since },
    );
    await expect(error.getByRole("button", { name: "다시 시도" })).toHaveCount(0);
  });

  test("답변에 피드백을 남길 수 있다", async ({ page }) => {
    const question = `${CODE} 프로젝트 마감일 알려줘 (피드백)`;
    const since = backendLogMark();

    await page.goto("/");
    await ask(page, question);
    await expect(page.getByText(/^\d+ms$/)).toBeVisible({ timeout: 10_000 });
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
      { since },
    );
    const answer = log(page).getByTestId("assistant-message").last();
    const like = answer.getByRole("button", { name: "좋아요" });
    const dislike = answer.getByRole("button", { name: "싫어요" });

    // 1) 싫어요 → 사유("실제로는 문서에 있음")와 메모 → 보내기
    await dislike.click();
    const form = answer.getByRole("form", { name: "싫어요 사유" });
    await expect(form.getByLabel("틀림")).toBeVisible();
    await expect(form.getByLabel("기타")).toBeVisible();
    await form.getByLabel("실제로는 문서에 있음").check();
    await form.getByLabel("메모").fill("담당 문서 2쪽에 있음");
    await form.getByRole("button", { name: "피드백 보내기" }).click();
    await expect(answer.getByText("피드백을 저장했습니다")).toBeVisible();
    await expect(form).toHaveCount(0);
    await expect(dislike).toHaveAttribute("aria-pressed", "true");

    const saved = await waitForBackendLog(
      (r) => r.event === "feedback.saved" && r.message_id === received.message_id,
      { since },
    );
    expect(saved.rating).toBe(-1);
    expect(saved.reason).toBe("not_found_wrong");
    expect(saved.has_note).toBe(true);
    expect(JSON.stringify(saved)).not.toContain("2쪽에 있음"); // 메모 원문은 로그에 없다

    // 2) 다시 누르면 덮어쓴다(좋아요)
    const mark = backendLogMark();
    await like.click();
    await expect(like).toHaveAttribute("aria-pressed", "true");
    await expect(dislike).toHaveAttribute("aria-pressed", "false");
    const overwritten = await waitForBackendLog(
      (r) => r.event === "feedback.saved" && r.message_id === received.message_id,
      { since: mark },
    );
    expect(overwritten.rating).toBe(1);
    expect(overwritten.reason).toBeNull();

    // 3) 새로고침해도 저장된 피드백이 보인다
    await page.reload();
    await expect(
      log(page).getByTestId("assistant-message").last().getByRole("button", { name: "좋아요" }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  test("입력창 위에 색인 범위가 보인다", async ({ page }) => {
    const since = backendLogMark();
    await page.goto("/");

    // 1) 화면: "키워드 검색 N · 의미 검색 M · 완료 예상 X월 Y일"(또는 임베딩 완료) 한 줄이 입력창 위에
    const line = page.getByTestId("index-coverage");
    await expect(line).toHaveText(
      /^키워드 검색 [\d,]+ · 의미 검색 [\d,]+ · (완료 예상 \d{1,2}월 \d{1,2}일|임베딩 완료)$/,
    );
    const lineBox = await line.boundingBox();
    const inputBox = await page.getByPlaceholder(/질문을 입력하세요/).boundingBox();
    expect(lineBox!.y).toBeLessThan(inputBox!.y);

    // 2) 백엔드: 색인 범위를 계산했고, 화면 숫자가 같다(이번 실행에서 넣은 문서 2개 이상)
    const read = await waitForBackendLog((r) => r.event === "index.coverage.read", { since });
    const fmt = (v: unknown) => new Intl.NumberFormat("ko-KR").format(v as number);
    expect(read.bm25_docs as number).toBeGreaterThanOrEqual(2);
    await expect(line).toContainText(
      `키워드 검색 ${fmt(read.bm25_docs)} · 의미 검색 ${fmt(read.vector_docs)}`,
    );
    await waitForBackendLog(
      (r) => r.event === "http.request" && r.path === "/api/index/coverage" && r.status === 200,
      { since },
    );
  });

  test("좁은 화면에서는 대화 목록과 출처 패널을 서랍으로 연다", async ({ page }) => {
    await page.setViewportSize({ width: 600, height: 900 });
    const question = `${CODE} 프로젝트 마감일 알려줘 (좁은 화면)`;
    const since = backendLogMark();
    await page.goto("/");

    // 1) 목록·패널은 처음에 숨겨져 있고 버튼으로 서랍을 연다
    await expect(page.getByRole("region", { name: "대화 목록" })).toHaveCount(0);
    await expect(page.getByRole("complementary", { name: "출처 패널" })).toHaveCount(0);
    await page.getByRole("button", { name: "대화 목록", exact: true }).click();
    const listDrawer = page.getByRole("dialog", { name: "대화 목록 서랍" });
    await expect(listDrawer.getByRole("region", { name: "대화 목록" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(listDrawer).toHaveCount(0);

    // 2) 인용 칩을 누르면 출처 서랍이 열리고 그 항목이 펼쳐져 있다
    await ask(page, question);
    const chip = log(page).locator("button[data-citation]").first();
    await expect(chip).toBeVisible({ timeout: 10_000 });
    await chip.click();
    const drawer = page.getByRole("dialog", { name: "출처 패널" });
    await expect(drawer.getByTestId("source-detail")).toBeVisible();
    await expect(drawer.getByTestId("source-detail")).toContainText(FILE);

    // 3) 백엔드: 답변 완료
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
      { since },
    );
    await waitForBackendLog(
      (r) => r.event === "chat.message.completed" && r.message_id === received.message_id,
      { since },
    );
  });
});
