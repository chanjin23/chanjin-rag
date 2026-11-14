import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { backendLogMark, waitForBackendLog } from "./backendLog";
import { BACKEND_LOG, TEST_DATABASE_URL, UPLOAD_TMP_DIR } from "./env";

/**
 * 평가 화면 (T15, F-12, F-13, PRD 8장, TRD 9.1 `/eval`, 14장 평가 CLI).
 * 시드: 합성 문서를 `rag-backend ingest-local`로 넣고, `rag-eval run`(Fake Provider·Fake 판정기)을
 * 평가셋 두 개로 차례로 실행한다. 두 번째 평가셋에는 위치가 맞지 않는 근거 라벨 문항이 있어
 * Recall@5가 첫 실행보다 낮다(이전 실행 대비 증감 확인). 경로가 없는 문항은 제외된다.
 * 테스트 DB는 실행 사이에 남으므로 실행마다 다른 코드명을 문서·질문에 넣는다.
 */
const ROOT = path.resolve(import.meta.dirname, ".tmp", "eval-root");
const DATASETS = path.resolve(import.meta.dirname, ".tmp", "eval-datasets");
const REPO = path.resolve(import.meta.dirname, "..", "..");

function codeName(seed: number): string {
  let n = seed;
  let out = "";
  while (n > 0) {
    out += String.fromCharCode(97 + (n % 26));
    n = Math.floor(n / 26);
  }
  return `ze${out}`;
}

const stamp = Date.now();
const CODE = codeName(stamp);
const DOC = path.join(ROOT, `평가-${CODE}.md`);
const QUESTION = `${CODE} 프로젝트의 마감일은 언제인가?`;

const COMMON_ENV = {
  ...process.env,
  APP_ENV: "test",
  PROVIDER: "fake",
  DATABASE_URL: TEST_DATABASE_URL,
  LOG_FORMAT: "json",
  LOG_FILE: BACKEND_LOG,
  PYTHONIOENCODING: "utf-8",
};

function ingestLocal(folder: string) {
  return spawnSync("uv", ["run", "rag-backend", "ingest-local", folder], {
    cwd: path.join(REPO, "backend"),
    encoding: "utf-8",
    env: { ...COMMON_ENV, SYNC_ALLOWED_ROOT: ROOT, UPLOAD_TMP_DIR, EMBED_DAILY_FILE_LIMIT: "100000" },
  });
}

function ragEval(dataset: string): number {
  const res = spawnSync("uv", ["run", "rag-eval", "run", "--dataset", dataset], {
    cwd: REPO,
    encoding: "utf-8",
    env: COMMON_ENV,
  });
  expect(res.status, `${res.stdout}\n${res.stderr}`).toBe(0);
  const m = /평가 실행 #(\d+) 시작/.exec(res.stdout);
  expect(m, res.stdout).not.toBeNull();
  return Number(m![1]);
}

/** YAML 작은따옴표 문자열 */
const q = (s: string) => `'${s.replace(/'/g, "''")}'`;

function dataset(name: string, extra: string): string {
  const file = path.join(DATASETS, `${name}.yaml`);
  writeFileSync(
    file,
    [
      "version: 1",
      "items:",
      "  - id: deadline",
      "    type: single",
      `    question: ${q(QUESTION)}`,
      `    answer: ${q(`${CODE} 프로젝트의 마감일은 12월 15일이다.`)}`,
      "    evidence:",
      `      - path: ${q(DOC)}`,
      "  - id: none",
      "    type: not_found",
      "    question: 화성 탐사선의 연료 탱크 용량은 얼마인가?",
      extra,
    ].join("\n"),
    "utf-8",
  );
  return file;
}

let firstRun = 0;
let secondRun = 0;

const runList = (page: Page) => page.getByRole("list", { name: "평가 실행 목록" });
const metricsTable = (page: Page) => page.getByRole("table", { name: "지표 표" });

test.describe("평가 화면 (F-12, F-13)", () => {
  test.beforeAll(() => {
    test.setTimeout(180_000);
    rmSync(ROOT, { recursive: true, force: true });
    rmSync(DATASETS, { recursive: true, force: true });
    mkdirSync(ROOT, { recursive: true });
    mkdirSync(DATASETS, { recursive: true });
    writeFileSync(
      DOC,
      `# ${CODE} 프로젝트 계획\n\n${CODE} 프로젝트의 마감일은 12월 15일이다.\n\n${CODE} 프로젝트 담당자는 기획팀이다.\n`,
      "utf-8",
    );
    const ingest = ingestLocal(ROOT);
    expect(ingest.status, `${ingest.stdout}\n${ingest.stderr}`).toBe(0);

    firstRun = ragEval(dataset("first", ""));
    // 두 번째: 위치가 맞지 않는 근거 라벨(검색돼도 겹치지 않음) + 경로가 없는 문항(제외)
    secondRun = ragEval(
      dataset(
        "second",
        [
          "  - id: wrong-location",
          "    type: single",
          `    question: ${q(`${CODE} 프로젝트 담당자는 누구인가?`)}`,
          `    answer: ${q(`${CODE} 프로젝트 담당자는 기획팀이다.`)}`,
          "    evidence:",
          `      - path: ${q(DOC)}`,
          "        location: {lines: [900, 910]}",
          "  - id: moved",
          "    type: single",
          "    question: 옮긴 문서에는 무엇이 있나?",
          "    answer: 없음",
          "    evidence:",
          `      - path: ${q(path.join(ROOT, `사라진-${CODE}.md`))}`,
        ].join("\n"),
      ),
    );
    expect(secondRun).toBeGreaterThan(firstRun);
  });

  test("평가 실행 목록과 지표가 보인다", async ({ page }) => {
    const completed = await waitForBackendLog(
      (r) => r.event === "eval.run.completed" && r.run_id === firstRun,
    );
    expect(completed.recall_at_5).toBe(1);

    const since = backendLogMark();
    await page.goto("/eval");
    await expect(page.getByRole("heading", { level: 1, name: "평가" })).toBeVisible();
    await expect(runList(page).getByRole("listitem", { name: `실행 #${firstRun}` })).toBeVisible();
    await expect(runList(page).getByRole("listitem", { name: `실행 #${secondRun}` })).toBeVisible();

    // 이전 실행을 고르면 그 실행의 지표 표가 보인다
    await runList(page).getByRole("button", { name: new RegExp(`실행 #${firstRun}\\b`) }).click();
    await expect(page.getByRole("heading", { name: new RegExp(`실행 #${firstRun} 지표`) })).toBeVisible();
    const table = metricsTable(page);
    for (const label of ["Recall@5", "MRR", "근거 충실성", "답변 정확도", "찾지 못함 정확도", "p95 응답 시간(ms)"]) {
      await expect(table.getByRole("rowheader", { name: label, exact: true })).toBeVisible();
    }
    const recall = table.getByRole("row", { name: /^Recall@5/ });
    await expect(recall.getByRole("cell").first()).toHaveText("1.000");
    await expect(table.getByRole("row", { name: /^찾지 못함 정확도/ }).getByRole("cell").first()).toHaveText(
      "1.000",
    );
    await expect(page.getByText("판정 모델 fake-judge", { exact: false })).toBeVisible();
    await expect(page.getByRole("listitem", { name: "문항 deadline" })).toContainText("판정 정답");

    await waitForBackendLog((r) => r.event === "eval.run.read" && r.run_id === firstRun, { since });
    await waitForBackendLog((r) => r.event === "eval.runs.listed", { since });
  });

  test("이전 실행 대비 증감이 보인다", async ({ page }) => {
    const since = backendLogMark();
    await page.goto("/eval");
    await runList(page).getByRole("button", { name: new RegExp(`실행 #${secondRun}\\b`) }).click();
    await expect(page.getByText(`이전 실행 #${firstRun} 대비`)).toBeVisible();

    const table = metricsTable(page);
    // 위치가 맞지 않는 라벨 하나 때문에 Recall@5가 1.000 → 0.500
    const recall = table.getByRole("row", { name: /^Recall@5/ });
    await expect(recall.getByRole("cell").nth(0)).toHaveText("0.500");
    await expect(recall.getByRole("cell").nth(1)).toHaveText("1.000");
    await expect(table.getByLabel("Recall@5 증감")).toHaveText("-0.500");
    await expect(table.getByLabel("Recall@5 증감")).toHaveClass(/text-rose-700/);
    await expect(table.getByLabel("찾지 못함 정확도 증감")).toHaveText("±0.000");

    // 경로가 없는 문항은 제외로 표시된다
    await expect(page.getByRole("listitem", { name: "문항 moved" })).toContainText("제외");
    await expect(page.getByRole("listitem", { name: "문항 wrong-location" })).toContainText("Recall@5 0");

    const read = await waitForBackendLog((r) => r.event === "eval.run.read" && r.run_id === secondRun, {
      since,
    });
    expect(read.previous_id).toBe(firstRun);
    const excluded = await waitForBackendLog((r) => r.event === "eval.item.excluded" && r.item_id === "moved");
    expect(excluded.level).toBe("warning");
  });

  test("싫어요 피드백에 실패 유형을 붙일 수 있다", async ({ page }) => {
    // 채팅에서 싫어요를 남긴다
    const question = `${CODE} 프로젝트 담당자는 누구야?`;
    await page.goto("/c/new");
    const input = page.getByPlaceholder(/질문을 입력하세요/);
    await input.fill(question);
    await input.press("Enter");
    const chat = page.getByRole("log", { name: "대화 내용" });
    await expect(chat.getByText(question, { exact: true })).toBeVisible();
    const answer = chat.getByTestId("assistant-message").last();
    await expect(answer.getByRole("button", { name: "싫어요" })).toBeVisible({ timeout: 15_000 });
    await answer.getByRole("button", { name: "싫어요" }).click();
    const form = answer.getByRole("form", { name: "싫어요 사유" });
    await form.getByLabel("틀림").check();
    await form.getByLabel("메모").fill(`검토용 ${CODE}`);
    await form.getByRole("button", { name: "피드백 보내기" }).click();
    await expect(answer.getByText("피드백을 저장했습니다")).toBeVisible();

    // 평가 화면에서 실패 유형을 붙인다
    const since = backendLogMark();
    await page.goto("/eval");
    const review = page.getByRole("region", { name: "싫어요 피드백 검토" });
    const item = review.getByRole("listitem").filter({ hasText: `검토용 ${CODE}` });
    await expect(item).toHaveCount(1);
    await expect(item.getByText(question)).toBeVisible();
    await expect(item.getByText("사유: 틀림")).toBeVisible();
    await expect(item.getByRole("status")).toHaveText("검토 전");

    await item.getByLabel("실패 유형").selectOption({ label: "검색 누락" });
    await item.getByRole("button", { name: "유형 저장" }).click();
    await expect(item.getByRole("status")).toContainText("검토함 · 검색 누락");

    const reviewed = await waitForBackendLog((r) => r.event === "feedback.reviewed", { since });
    expect(reviewed.failure_type).toBe("retrieval");
    expect(reviewed.rating).toBe(-1);

    // 다시 열어도 검토 결과가 남아 있다
    await page.reload();
    const again = page
      .getByRole("region", { name: "싫어요 피드백 검토" })
      .getByRole("listitem")
      .filter({ hasText: `검토용 ${CODE}` });
    await expect(again.getByRole("status")).toContainText("검토함 · 검색 누락");
    await expect(again.getByLabel("실패 유형")).toHaveValue("retrieval");
  });
});
