import { expect, test } from "@playwright/test";

test.describe("기본 화면", () => {
  test("백엔드 health가 웹 프록시를 통해 응답한다", async ({ request }) => {
    const res = await request.get("/api/health");
    expect(res.ok()).toBe(true);
    // 공개 요약: 프로세스가 살아 있으면 status "ok", 이상은 코드만(세부는 세션이 필요한 /health/details)
    const body = await res.json();
    expect(body.status).toBe("ok");
    expect(Object.keys(body).sort()).toEqual(["degraded", "issues", "status"]);
    expect(typeof body.degraded).toBe("boolean");
    for (const issue of body.issues) expect(Object.keys(issue)).toEqual(["code"]);
    expect(body.issues.map((i: { code: string }) => i.code)).not.toContain("db_unavailable");
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
