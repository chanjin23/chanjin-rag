import { expect, test } from "@playwright/test";

test.describe("기본 화면", () => {
  test("백엔드 health가 웹 프록시를 통해 응답한다", async ({ request }) => {
    const res = await request.get("/api/health");
    expect(res.ok()).toBe(true);
    expect(await res.json()).toEqual({ status: "ok" });
  });

  test("상단 메뉴로 각 화면에 이동할 수 있다", async ({ page }) => {
    await page.goto("/");
    const nav = page.getByRole("navigation");

    for (const [label, heading] of [
      ["문서", "문서"],
      ["동기화", "동기화"],
      ["평가", "평가"],
    ] as const) {
      await nav.getByRole("link", { name: label }).click();
      await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    }

    await nav.getByRole("link", { name: "채팅" }).click();
    await expect(page.getByPlaceholder(/질문을 입력하세요/)).toBeVisible();
  });
});
