import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { backendLogMark, waitForBackendLog } from "./backendLog";
import { BACKEND_LOG, TEST_DATABASE_URL, UPLOAD_TMP_DIR, WEB_URL } from "./env";

/**
 * 문서 화면 (T12, F-05, F-10, F-19, TRD 3.4, 5.1, 6.5, 9.1): 상태 탭, 실패 사유·해결 안내, 재처리,
 * 영구 삭제, 인용 → 문서 상세 위치 강조.
 * 시드: `rag-backend ingest-local`로 실제 수집 경로(파싱 → 청킹 → Kiwi → Fake 임베딩)를 거친 합성 문서.
 * - 정상 MD(완료), 손상된 DOCX(실패: corrupted), 빈 TXT(텍스트 없음), 삭제용 MD
 * 테스트 DB는 실행 사이에 남으므로 실행마다 다른 코드명을 파일명·본문에 넣고 검색어로 이번 문서만 본다.
 */
const ROOT = path.resolve(import.meta.dirname, ".tmp", "documents-root");

function codeName(seed: number): string {
  let n = seed;
  let out = "";
  while (n > 0) {
    out += String.fromCharCode(97 + (n % 26));
    n = Math.floor(n / 26);
  }
  return `zd${out}`;
}

const stamp = Date.now();
const CODE = codeName(stamp);
/** 인용 위치 강조용 문단에만 있는 낱말 */
const TARGET = codeName(stamp + 3);
const DEL_CODE = codeName(stamp + 5);
const READY_FILE = `계획-${CODE}.md`;
const BROKEN_FILE = `손상-${CODE}.docx`;
const EMPTY_FILE = `빈문서-${CODE}.txt`;
const DEL_FILE = `회의-${DEL_CODE}.md`;

const log = (page: Page) => page.getByRole("log", { name: "대화 내용" });
const docList = (page: Page) => page.getByRole("list", { name: "문서 목록" });
const row = (page: Page, file: string) => docList(page).getByRole("listitem", { name: file });

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
      // 테스트 DB는 E2E 실행 사이에 남으므로, 같은 날 여러 번 돌려도 하루 임베딩 한도에 막혀
      // "완료"가 아닌 "BM25만"으로 남지 않게 한도를 넉넉히 둔다(Fake 임베딩이라 외부 호출 없음)
      EMBED_DAILY_FILE_LIMIT: "100000",
      PYTHONIOENCODING: "utf-8",
    },
  });
}

async function ask(page: Page, question: string) {
  const input = page.getByPlaceholder(/질문을 입력하세요/);
  await input.fill(question);
  await input.press("Enter");
  await expect(log(page).getByText(question, { exact: true })).toBeVisible();
  await expect(page.getByText(/^\d+ms$/).last()).toBeVisible({ timeout: 15_000 });
}

/** 문서 화면을 이번 실행의 검색어로 연다 */
async function openDocuments(page: Page, q: string) {
  await page.goto("/documents");
  await expect(page.getByRole("heading", { level: 1, name: "문서" })).toBeVisible();
  await page.getByLabel("문서 검색어").fill(q);
  await page.getByRole("button", { name: "찾기" }).click();
  await expect(page).toHaveURL(new RegExp(`[?&]q=${q}`));
}

test.describe("문서 화면 (F-05, F-10, F-19)", () => {
  test.beforeAll(() => {
    test.setTimeout(120_000);
    rmSync(ROOT, { recursive: true, force: true });
    const folder = path.join(ROOT, `docs-${stamp}`);
    mkdirSync(folder, { recursive: true });
    // 제목마다 섹션이 나뉘어 청크도 나뉜다. 강조할 문단은 맨 아래(스크롤해야 보이는 곳)에 둔다
    const filler = Array.from(
      { length: 20 },
      (_, i) => `## ${i + 1}장 준비\n\n${CODE} 계획의 ${i + 1}번째 준비 항목은 일반 점검이다.\n`,
    ).join("\n");
    writeFileSync(
      path.join(folder, READY_FILE),
      `# ${CODE} 계획\n\n${filler}\n## 예산\n\n${TARGET} 예산은 3억 원으로 확정했다.\n`,
    );
    // ZIP이 아닌 DOCX → 손상(corrupted)
    writeFileSync(path.join(folder, BROKEN_FILE), "이것은 DOCX가 아닌 손상된 파일이다");
    writeFileSync(path.join(folder, EMPTY_FILE), "");
    writeFileSync(
      path.join(folder, DEL_FILE),
      `# ${DEL_CODE} 회의\n\n${DEL_CODE} 회의 결론은 출시를 다음 달로 미루는 것이다.\n`,
    );
    const res = ingestLocal(folder);
    expect(res.status, res.stderr).toBe(0);
    expect(res.stdout).toContain("접수: 새 버전 4");
  });

  test("상태 탭으로 문서를 거를 수 있다", async ({ page }) => {
    const since = backendLogMark();
    await openDocuments(page, CODE);

    // 전체: 이번 실행의 세 문서(삭제용 문서는 코드명이 다름)
    const tabs = page.getByRole("tablist", { name: "문서 상태" });
    for (const label of ["대기", "처리 중", "BM25만", "완료", "텍스트 없음", "실패", "파일명만", "로컬 삭제"]) {
      await expect(tabs.getByRole("tab", { name: new RegExp(`^${label}`) })).toBeVisible();
    }
    await expect(row(page, READY_FILE)).toBeVisible();
    await expect(row(page, BROKEN_FILE)).toBeVisible();
    await expect(row(page, EMPTY_FILE)).toBeVisible();

    // 실패 탭
    await tabs.getByRole("tab", { name: /^실패/ }).click();
    await expect(tabs.getByRole("tab", { name: /^실패/ })).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/[?&]tab=failed/);
    await expect(row(page, BROKEN_FILE)).toBeVisible();
    await expect(row(page, BROKEN_FILE)).toContainText("실패");
    await expect(row(page, READY_FILE)).toHaveCount(0);
    await expect(row(page, EMPTY_FILE)).toHaveCount(0);

    // 완료 탭
    await tabs.getByRole("tab", { name: /^완료/ }).click();
    await expect(row(page, READY_FILE)).toBeVisible();
    await expect(row(page, READY_FILE)).toContainText("완료");
    await expect(row(page, BROKEN_FILE)).toHaveCount(0);

    // 텍스트 없음 탭
    await tabs.getByRole("tab", { name: /^텍스트 없음/ }).click();
    await expect(row(page, EMPTY_FILE)).toBeVisible();
    await expect(row(page, READY_FILE)).toHaveCount(0);

    // 로컬 삭제 탭: 이번 문서는 없다
    await tabs.getByRole("tab", { name: /^로컬 삭제/ }).click();
    await expect(page.getByText("해당하는 문서가 없습니다.")).toBeVisible();

    // 백엔드: 상태 필터·로컬 삭제 필터와 검색어로 목록을 조회했다
    const statusQuery = await waitForBackendLog(
      (r) =>
        r.event === "documents.listed" &&
        Array.isArray(r.filters) &&
        r.filters.includes("status") &&
        r.filters.includes("q") &&
        r.total === 1,
      { since },
    );
    expect(JSON.stringify(statusQuery)).not.toContain(CODE); // 검색어 원문은 남기지 않는다
    await waitForBackendLog(
      (r) =>
        r.event === "documents.listed" &&
        Array.isArray(r.filters) &&
        r.filters.includes("locally_deleted") &&
        r.total === 0,
      { since },
    );
  });

  test("실패 문서의 사유와 해결 안내가 보인다", async ({ page }) => {
    const since = backendLogMark();
    await openDocuments(page, CODE);
    await page.getByRole("tablist", { name: "문서 상태" }).getByRole("tab", { name: /^실패/ }).click();

    const failure = row(page, BROKEN_FILE).getByTestId("failure");
    await expect(failure).toContainText("실패 사유: 파일 손상, 형식 불일치");
    await expect(failure).toContainText("(corrupted)");
    await expect(failure).toContainText("해결 안내: 파일을 다시 저장해 보세요");
    await expect(row(page, BROKEN_FILE).getByRole("button", { name: "재처리" })).toBeVisible();

    // 문서 상세에도 사유와 안내가 보인다(추출 텍스트는 없음)
    await row(page, BROKEN_FILE).getByRole("link", { name: BROKEN_FILE }).click();
    await expect(page.getByRole("heading", { level: 1, name: BROKEN_FILE })).toBeVisible();
    await expect(page.getByText("해결 안내: 파일을 다시 저장해 보세요")).toBeVisible();
    await expect(page.getByText("저장된 추출 텍스트가 없습니다.")).toBeVisible();

    // 백엔드: 실패 상태로 거른 목록과 상세 조회
    await waitForBackendLog(
      (r) =>
        r.event === "documents.listed" &&
        Array.isArray(r.filters) &&
        r.filters.includes("status") &&
        r.total === 1,
      { since },
    );
    const id = Number(new URL(page.url()).pathname.split("/").pop());
    await waitForBackendLog((r) => r.event === "documents.read" && r.document_id === id, { since });
  });

  test("재처리하면 다음 동기화 대기로 표시된다", async ({ page }) => {
    const since = backendLogMark();
    await openDocuments(page, CODE);
    await page.getByRole("tablist", { name: "문서 상태" }).getByRole("tab", { name: /^실패/ }).click();
    const target = row(page, BROKEN_FILE);
    await expect(target.getByText("다음 동기화 대기")).toHaveCount(0);

    await target.getByRole("button", { name: "재처리" }).click();

    // 화면: "다음 동기화 대기"와 "지금 동기화" 버튼, 재처리 버튼은 사라진다
    await expect(target.getByText("다음 동기화 대기")).toBeVisible();
    await expect(target.getByRole("button", { name: "재처리" })).toHaveCount(0);
    const syncNow = target.getByRole("button", { name: "지금 동기화" });
    await expect(syncNow).toBeVisible();

    // 백엔드: 재처리 요청 로그(재업로드 표시)
    const requested = await waitForBackendLog(
      (r) => r.event === "documents.reprocess.requested" && r.action === "needs_upload",
      { since },
    );
    expect(requested.previous_status).toBe("failed");
    expect(requested.failure_code).toBe("corrupted");
    expect(JSON.stringify(requested)).not.toContain(CODE);

    // 새로고침해도 유지된다(서버의 needs_upload)
    await page.reload();
    await expect(row(page, BROKEN_FILE).getByText("다음 동기화 대기")).toBeVisible();
    const res = await page.request.get(`/api/documents/${requested.document_id as number}`);
    expect((await res.json()).needs_upload).toBe(true);

    // "지금 동기화"는 동기화 화면으로 이동한다
    await row(page, BROKEN_FILE).getByRole("button", { name: "지금 동기화" }).click();
    await expect(page).toHaveURL(`${WEB_URL}/sync`);
    await expect(page.getByRole("heading", { level: 1, name: "동기화" })).toBeVisible();
  });

  test("영구 삭제하면 목록과 검색에서 사라진다", async ({ page }) => {
    test.setTimeout(60_000);
    const question = `${DEL_CODE} 회의 결론 알려줘`;
    const since = backendLogMark();

    // 1) 삭제 전: 질문하면 이 문서를 출처로 답한다
    await page.goto("/");
    await ask(page, question);
    const sources = page.getByLabel("출처", { exact: true });
    await expect(sources).toContainText(DEL_FILE);
    await expect(page).toHaveURL(/\/c\/\d+$/);
    const conversationUrl = page.url();

    // 2) 문서 화면에서 영구 삭제: 확인 창에 백업·외부 전송 안내
    await openDocuments(page, DEL_CODE);
    await row(page, DEL_FILE).getByRole("button", { name: "영구 삭제" }).click();
    const dialog = page.getByRole("alertdialog", { name: "문서를 영구 삭제할까요?" });
    await expect(dialog).toContainText(DEL_FILE);
    await expect(dialog).toContainText(
      "백업 파일에는 최대 7일(PC)까지 남습니다. 이미 NVIDIA API로 보낸 텍스트는 되돌릴 수 없습니다.",
    );
    // 취소하면 그대로 남는다
    await dialog.getByRole("button", { name: "취소" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(row(page, DEL_FILE)).toBeVisible();

    await row(page, DEL_FILE).getByRole("button", { name: "영구 삭제" }).click();
    await dialog.getByRole("button", { name: "영구 삭제" }).click();
    await expect(page.getByRole("status")).toContainText(`「${DEL_FILE}」을(를) 영구 삭제했습니다.`);
    await expect(row(page, DEL_FILE)).toHaveCount(0);
    await expect(page.getByText("해당하는 문서가 없습니다.")).toBeVisible();

    // 백엔드: 원인과 개수만 남긴 삭제 로그(예전 답변 1개의 인용을 가림)
    const purged = await waitForBackendLog(
      (r) => r.event === "documents.purged" && r.reason === "user",
      { since },
    );
    expect(purged.documents).toBe(1);
    expect(purged.chunks).toBeGreaterThan(0);
    expect(purged.masked_messages).toBe(1);
    expect(JSON.stringify(purged)).not.toContain(DEL_CODE);
    await waitForBackendLog(
      (r) => r.event === "http.request" && r.method === "DELETE" && String(r.path).startsWith("/api/documents/") && r.status === 204,
      { since },
    );

    // 3) 검색에서도 사라진다: 같은 질문에 이 문서가 출처·참고 후보에 없다
    const again = backendLogMark();
    await page.goto("/");
    await ask(page, question);
    await expect(page.getByRole("complementary", { name: "출처 패널" })).not.toContainText(DEL_FILE);
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
      { since: again },
    );
    await waitForBackendLog(
      (r) =>
        (r.event === "chat.message.completed" || r.event === "chat.message.not_found") &&
        r.message_id === received.message_id,
      { since: again },
    );

    // 4) 예전 대화의 인용 구절은 "삭제된 문서"로 가려져 있다
    await page.goto(conversationUrl);
    const item = page.getByLabel("출처", { exact: true }).getByRole("listitem").filter({ hasText: DEL_FILE }).first();
    await item.getByRole("button").first().click();
    await expect(item.getByLabel("인용 구절")).toHaveText("삭제된 문서");
    await expect(item).toContainText("영구 삭제된 문서입니다");
    await expect(item).not.toContainText("출시를 다음 달로");
  });

  test("인용에서 문서 상세로 이동하면 위치가 강조된다", async ({ page }) => {
    test.setTimeout(60_000);
    const question = `${TARGET} 예산은 얼마야`;
    const since = backendLogMark();

    await page.goto("/");
    await ask(page, question);
    const answer = log(page).getByTestId("assistant-message").last();
    await expect(answer).toContainText(/3억 원으로 확정했다\. ?\[\d\]/);

    // 1) 그 문장의 인용 칩 → 출처 항목 → "문서 상세"(?loc=로 인용 위치를 넘김)
    const n = (await answer.textContent())?.match(/확정했다\. ?\[(\d)\]/)?.[1];
    expect(n).toBeTruthy();
    await answer.locator(`button[data-citation="${n}"]`).first().click();
    const item = page.getByLabel("출처", { exact: true }).getByRole("listitem").filter({ hasText: `[${n}]` });
    await expect(item).toContainText(READY_FILE);
    const link = item.getByRole("link", { name: "문서 상세" });
    await expect(link).toHaveAttribute("href", /^\/documents\/\d+\?loc=.+$/);
    await link.click();

    // 2) 상세: 추출 텍스트 중 인용 위치가 강조되고 화면 안으로 스크롤된다
    await expect(page).toHaveURL(/\/documents\/\d+\?loc=/);
    await expect(page.getByRole("heading", { level: 1, name: READY_FILE })).toBeVisible();
    const sections = page.getByRole("list", { name: "추출 텍스트" }).getByRole("listitem");
    await expect(sections).toHaveCount(21);
    const highlighted = page.locator('[data-highlight="true"]');
    await expect(highlighted).toHaveCount(1);
    await expect(highlighted).toContainText(`${TARGET} 예산은 3억 원으로 확정했다.`);
    await expect(highlighted).toContainText("인용 위치");
    await expect(highlighted).toHaveAttribute("aria-current", "location");
    await expect(highlighted).toBeInViewport();
    await expect(sections.first()).not.toBeInViewport();
    // 인용 이후 바뀌지 않은 문서라 변경 안내는 없다
    await expect(page.getByText("원문이 바뀌었거나 삭제되었습니다")).toHaveCount(0);

    // 백엔드: 상세 조회
    const id = Number(new URL(page.url()).pathname.split("/").pop());
    await waitForBackendLog((r) => r.event === "documents.read" && r.document_id === id, { since });

    // 3) "이 문서 요약" → 채팅에서 summarize_document_id로 요약
    const summarySince = backendLogMark();
    await page.getByRole("button", { name: "이 문서 요약" }).click();
    await expect(log(page).getByText(`「${READY_FILE}」 요약해줘`, { exact: true })).toBeVisible();
    await expect(page.getByText(/^\d+ms$/).last()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByLabel("출처", { exact: true })).toContainText(READY_FILE);
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.summarize_document_id === id,
      { since: summarySince },
    );
    await waitForBackendLog(
      (r) => r.event === "chat.message.completed" && r.message_id === received.message_id,
      { since: summarySince },
    );
  });
});
