import { expect, test } from "@playwright/test";
import { waitForBackendLog } from "./backendLog";

test.describe("채팅 (F-08 흐름의 뼈대)", () => {
  test("화면에서 보낸 질문이 백엔드 로그에 남고, 답변이 스트리밍된다", async ({ page }) => {
    // 실행마다 다른 질문을 써서 로그에서 이번 요청만 정확히 찾는다
    const question = `작년 계약서의 위약금 조항 알려줘 #${Date.now()}`;

    await page.goto("/");
    const input = page.getByPlaceholder(/질문을 입력하세요/);
    await input.fill(question);
    await input.press("Enter");

    // 1) 화면: 내 질문 말풍선, 스트리밍된 답변, 지연 시간 표시
    await expect(page.getByText(question, { exact: true })).toBeVisible();
    await expect(page.getByText(`백엔드가 질문을 받았습니다: ${question}`)).toBeVisible();
    await expect(page.getByText(/^\d+ms$/)).toBeVisible();
    await expect(input).toHaveValue("");

    // 2) 백엔드: 요청을 받아 로그에 기록했는지
    const received = await waitForBackendLog(
      (r) => r.event === "chat.message.received" && r.text === question,
    );
    expect(received.conversation_id).toBe("draft");
    expect(received.length).toBe(question.length);

    await waitForBackendLog(
      (r) =>
        r.event === "http.request" &&
        r.method === "POST" &&
        r.path === "/api/conversations/draft/messages" &&
        r.status === 200,
    );
    await waitForBackendLog((r) => r.event === "chat.message.completed");
  });

  test("빈 입력은 보내지 않는다", async ({ page }) => {
    await page.goto("/");
    await page.getByPlaceholder(/질문을 입력하세요/).fill("   ");
    await expect(page.getByRole("button", { name: "전송" })).toBeDisabled();
  });
});
