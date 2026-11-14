/**
 * 공용 시각 표시(src/lib/time.ts) 단위 테스트. 실행: `corepack pnpm test`(node --test).
 * 한국 시간(UTC+9) 기준이므로 실행하는 PC의 시간대와 상관없이 같은 결과여야 한다.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  formatDate,
  formatDateTime,
  formatMonthDay,
  formatRelative,
  formatRelativeDateTime,
} from "../src/lib/time.ts";

const NOW = Date.parse("2026-11-14T06:00:00Z"); // 한국 시간 2026-11-14 15:00
const ago = (ms: number) => new Date(NOW - ms).toISOString();
const SEC = 1000;
const MIN = 60 * SEC;
const HOUR = 60 * MIN;

describe("formatRelative: 하루 이내 상대 시각", () => {
  test("1분 미만은 방금", () => {
    assert.equal(formatRelative(ago(0), NOW), "방금");
    assert.equal(formatRelative(ago(59 * SEC), NOW), "방금");
  });
  test("1분~59분은 N분 전", () => {
    assert.equal(formatRelative(ago(60 * SEC), NOW), "1분 전");
    assert.equal(formatRelative(ago(59 * MIN + 59 * SEC), NOW), "59분 전");
  });
  test("60분~23시간은 N시간 전", () => {
    assert.equal(formatRelative(ago(60 * MIN), NOW), "1시간 전");
    assert.equal(formatRelative(ago(23 * HOUR), NOW), "23시간 전");
    assert.equal(formatRelative(ago(24 * HOUR - 1), NOW), "23시간 전");
  });
  test("24시간 이상은 한국 시간 날짜", () => {
    assert.equal(formatRelative(ago(24 * HOUR), NOW), "2026. 11. 13.");
  });
  test("미래 시각은 상대 시각 대신 날짜", () => {
    assert.equal(formatRelative(new Date(NOW + 5 * MIN).toISOString(), NOW), "2026. 11. 14.");
  });
});

describe("한국 시간 날짜 경계", () => {
  test("UTC 15:00은 한국 시간 다음 날 0시", () => {
    assert.equal(formatDate("2026-11-14T14:59:00Z"), "2026. 11. 14.");
    assert.equal(formatDate("2026-11-14T15:00:00Z"), "2026. 11. 15.");
  });
  test("formatDateTime은 한국 시간 월·일·시각(24시간제)", () => {
    assert.equal(formatDateTime("2026-11-14T15:05:00Z", NOW), "11월 15일 00:05");
    assert.equal(formatDateTime("2026-11-01T03:30:00Z", NOW), "11월 1일 12:30");
  });
  test("다른 해는 연도를 붙인다(한국 시간 기준 해)", () => {
    assert.equal(formatDateTime("2025-03-02T01:00:00Z", NOW), "2025년 3월 2일 10:00");
    // UTC로는 2025년 12월 31일이지만 한국 시간으로는 2026년 1월 1일 → 같은 해
    assert.equal(formatDateTime("2025-12-31T16:00:00Z", NOW), "1월 1일 01:00");
  });
});

describe("formatRelativeDateTime", () => {
  test("하루 이내는 상대 시각, 넘으면 날짜와 시각", () => {
    assert.equal(formatRelativeDateTime(ago(3 * HOUR), NOW), "3시간 전");
    assert.equal(formatRelativeDateTime(ago(2 * 24 * HOUR), NOW), "11월 12일 15:00");
  });
});

test("formatMonthDay: YYYY-MM-DD → M월 D일", () => {
  assert.equal(formatMonthDay("2026-11-04"), "11월 4일");
});
