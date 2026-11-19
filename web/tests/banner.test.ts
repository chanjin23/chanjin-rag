/** 전역 배너 닫음 키(src/lib/banner.ts) 단위 테스트. 실행: `corepack pnpm test`. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { issueKey, pruneDismissed } from "../src/lib/banner.ts";

const auth = { code: "provider_auth", since: "2026-11-14T06:00:00Z", level: null };

test("같은 장애(같은 since)면 키가 같아 닫은 배너가 숨은 채로 있다", () => {
  assert.equal(issueKey(auth), issueKey({ ...auth }));
});

test("since나 level이 바뀌면 키가 달라져 다시 보인다", () => {
  assert.notEqual(issueKey(auth), issueKey({ ...auth, since: "2026-11-14T07:00:00Z" }));
  const disk = { code: "disk_low", since: "2026-11-14T06:00:00Z", level: 5 };
  assert.notEqual(issueKey(disk), issueKey({ ...disk, level: 3 }));
});

test("사라진 이상의 닫음 기록은 지운다(다시 생기면 다시 보이게)", () => {
  const disk = issueKey({ code: "disk_low", since: "x", level: 5 });
  assert.deepEqual(pruneDismissed([issueKey(auth), disk], [disk]), [disk]);
  assert.deepEqual(pruneDismissed([issueKey(auth)], []), []);
});
