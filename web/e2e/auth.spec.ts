import { expect, type Page, test } from "@playwright/test";
import { backendLogMark, findBackendLogs, waitForBackendLog } from "./backendLog";
import { E2E_PASSWORD, E2E_USER, WEB_URL } from "./env";

/**
 * 로그인 (TRD 8.1, 9.1 `/login`, 11장). 공유 세션 없이 빈 상태에서 시작한다.
 * 잠금 테스트는 E2E 사용자를 15분 잠그므로 이 파일의 맨 끝에 둔다. 잠겨도 이미 발급된 세션
 * (setup 프로젝트의 storageState)은 유효해서 다른 테스트에는 영향이 없다.
 * 다음 실행의 global-setup이 `reset-password`로 잠금을 푼다.
 */
test.use({ storageState: { cookies: [], origins: [] } });

async function submitLogin(page: Page, password: string) {
  await page.getByLabel("아이디").fill(E2E_USER);
  await page.getByLabel("비밀번호").fill(password);
  await page.getByRole("button", { name: "로그인" }).click();
}

test("로그인하면 채팅 화면으로 이동한다", async ({ page, context }) => {
  const since = backendLogMark();

  // 로그인하지 않았으면 원래 가려던 화면을 next로 붙여 로그인 화면으로 보낸다
  await page.goto("/sync");
  await expect(page).toHaveURL(/\/login\?next=%2Fsync$/);
  await expect(page.getByText(/rag-backend reset-password/)).toBeVisible();
  await waitForBackendLog(
    (r) => r.event === "auth.session.rejected" && r.path === "/api/auth/me" && r.reason === "missing",
    { since },
  );

  // 채팅 화면에서 시작했을 때: 로그인하면 채팅 화면으로 돌아간다
  await page.goto("/");
  await expect(page).toHaveURL(/\/login\?next=%2F$/);
  await submitLogin(page, E2E_PASSWORD);

  await expect(page).toHaveURL(`${WEB_URL}/`);
  await expect(page.getByPlaceholder(/질문을 입력하세요/)).toBeVisible();
  await expect(page.getByRole("banner").getByText(E2E_USER, { exact: true })).toBeVisible();

  // 세션 쿠키: HttpOnly, SameSite=Strict (E2E는 로컬 HTTP라 Secure를 끔)
  const sid = (await context.cookies()).find((c) => c.name === "sid");
  expect(sid?.httpOnly).toBe(true);
  expect(sid?.sameSite).toBe("Strict");

  // 백엔드: 로그인 성공 로그와 응답
  const ok = await waitForBackendLog((r) => r.event === "auth.login.succeeded", { since });
  expect(typeof ok.user_id).toBe("number");
  await waitForBackendLog(
    (r) =>
      r.event === "http.request" &&
      r.method === "POST" &&
      r.path === "/api/auth/login" &&
      r.status === 200,
    { since },
  );

  // 새로고침해도 세션으로 로그인 상태가 유지된다
  const meMark = backendLogMark();
  await page.reload();
  await expect(page.getByPlaceholder(/질문을 입력하세요/)).toBeVisible();
  await waitForBackendLog(
    (r) => r.event === "http.request" && r.path === "/api/auth/me" && r.status === 200,
    { since: meMark },
  );
});

test("세션이 만료되면 입력 중인 질문이 보존된다", async ({ page, context }) => {
  // 새 대화(`new`)로 보내는 화면. 대화 ID는 첫 답변이 끝나면 백엔드가 정한다(T10)
  const conversation = "new";
  const question = `세션 만료 뒤에도 남아야 하는 질문 #${Date.now()}`;

  await page.goto(`/c/${conversation}`);
  await submitLogin(page, E2E_PASSWORD);
  const input = page.getByPlaceholder(/질문을 입력하세요/);
  await expect(input).toBeVisible();
  await expect(page).toHaveURL(`${WEB_URL}/c/${conversation}`);

  // 서버가 더는 알지 못하는 세션(만료·삭제된 세션과 같다)으로 바꾼다
  await context.addCookies([
    { name: "sid", value: "expired-session-cookie", url: WEB_URL, httpOnly: true, sameSite: "Strict" },
  ]);
  const since = backendLogMark();

  await input.fill(question);
  await input.press("Enter");

  // 1) 로그인 화면으로 이동하고 만료 안내를 보여준다
  await expect(page).toHaveURL(new RegExp(`/login\\?next=%2Fc%2F${conversation}&reason=expired$`));
  await expect(page.getByText(/로그인이 만료되었습니다/)).toBeVisible();

  // 2) 백엔드: 세션을 거절하고 질문은 처리하지 않았다
  await waitForBackendLog(
    (r) =>
      r.event === "auth.session.rejected" &&
      r.reason === "unknown" &&
      r.path === `/api/conversations/${conversation}/messages`,
    { since },
  );
  await waitForBackendLog(
    (r) =>
      r.event === "http.request" &&
      r.path === `/api/conversations/${conversation}/messages` &&
      r.status === 401,
    { since },
  );
  expect(findBackendLogs((r) => r.event === "chat.message.received", since)).toHaveLength(0);

  // 3) 다시 로그인하면 원래 대화로 돌아오고, 질문이 입력창에 그대로 있다
  await submitLogin(page, E2E_PASSWORD);
  await expect(page).toHaveURL(`${WEB_URL}/c/${conversation}`);
  await expect(input).toHaveValue(question);
  await waitForBackendLog((r) => r.event === "auth.login.succeeded", { since });

  // 4) 그대로 보내면 정상 처리된다(질의 그래프가 끝까지 돌아 답변이 완료된다)
  await input.press("Enter");
  // 사이드바 대화 제목에도 같은 글자가 생기므로 메시지 영역 안에서 찾는다
  const messages = page.getByRole("log", { name: "대화 내용" });
  await expect(messages.getByText(question, { exact: true })).toBeVisible();
  await expect(input).toHaveValue("");
  await expect(page.getByText(/^\d+ms$/)).toBeVisible({ timeout: 10_000 });
  const received = await waitForBackendLog(
    (r) => r.event === "chat.message.received" && r.text === question,
    { since },
  );
  expect(typeof received.conversation_id).toBe("number");
  await waitForBackendLog(
    (r) =>
      (r.event === "chat.message.completed" || r.event === "chat.message.not_found") &&
      r.message_id === received.message_id,
    { since },
  );

  // 보낸 뒤에는 보존본을 지운다(새로고침해도 다시 채우지 않음)
  await page.reload();
  await expect(page.getByPlaceholder(/질문을 입력하세요/)).toHaveValue("");
});

test("비밀번호를 5번 틀리면 잠금 남은 시간이 보인다", async ({ page }) => {
  const since = backendLogMark();
  await page.goto("/login");

  for (let i = 1; i <= 4; i++) {
    await submitLogin(page, `wrong-password-${i}`);
    await expect(page.getByText("아이디 또는 비밀번호가 올바르지 않습니다.")).toBeVisible();
    await expect(page.getByLabel("비밀번호")).toHaveValue("");
  }

  // 5번째 실패: 잠금과 남은 시간(15분)
  await submitLogin(page, "wrong-password-5");
  const lockNotice = page.getByRole("alert").filter({ hasText: "로그인이 잠겼습니다" });
  await expect(lockNotice).toBeVisible();
  await expect(lockNotice).toContainText(/1[45]분 \d+초 뒤에 다시 시도할 수 있습니다/);
  await expect(page.getByRole("button", { name: "로그인" })).toBeDisabled();
  await expect(page).toHaveURL(/\/login$/);

  // 백엔드: 실패 5번, 이번 실패로 잠김
  await waitForBackendLog((r) => r.event === "auth.login.locked" && r.just_locked === true, {
    since,
  });
  const failed = findBackendLogs((r) => r.event === "auth.login.failed", since);
  expect(failed).toHaveLength(5);
  expect(failed.every((r) => r.reason === "invalid_password")).toBe(true);
  const locked = findBackendLogs((r) => r.event === "auth.login.locked", since);
  expect(locked[0].retry_after_s).toBe(15 * 60);
  expect(findBackendLogs((r) => r.event === "auth.login.succeeded", since)).toHaveLength(0);
  await waitForBackendLog(
    (r) => r.event === "http.request" && r.path === "/api/auth/login" && r.status === 429,
    { since },
  );

  // 화면을 새로 열어 맞는 비밀번호를 넣어도 잠금 중이면 남은 시간을 보여준다
  await page.reload();
  await submitLogin(page, E2E_PASSWORD);
  await expect(lockNotice).toContainText(/분 \d+초 뒤에 다시 시도할 수 있습니다/);
  await waitForBackendLog((r) => r.event === "auth.login.locked" && r.just_locked === false, {
    since,
  });
});
