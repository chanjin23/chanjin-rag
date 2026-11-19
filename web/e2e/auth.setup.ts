import { expect, test as setup } from "@playwright/test";
import { E2E_PASSWORD, E2E_USER, STORAGE_STATE } from "./env";

/** E2E 사용자로 로그인해 세션 쿠키를 저장한다. 다른 테스트는 이 로그인 상태를 공유한다. */
setup("E2E 사용자로 로그인한다", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("아이디").fill(E2E_USER);
  await page.getByLabel("비밀번호").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "로그인" }).click();
  await expect(page.getByPlaceholder(/질문을 입력하세요/)).toBeVisible();
  await page.context().storageState({ path: STORAGE_STATE });
});
