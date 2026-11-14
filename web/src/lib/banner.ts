/** `HealthIssue`(api/health.ts) 중 키에 쓰는 부분. 단위 테스트(node --test)가 바로 읽도록 import 없이 둔다. */
type IssueLike = { code: string; since: string | null; level: number | null };

/**
 * 전역 배너가 닫음을 기억하는 키(TRD 9.1). since(같은 이상인지)나 level(디스크가 더 줄어듦 등)이
 * 바뀌면 키가 달라져 닫은 배너가 다시 보인다.
 */
export function issueKey(issue: IssueLike): string {
  return `${issue.code}|${issue.since ?? ""}|${issue.level ?? ""}`;
}

/** 지금 있는 이상의 키만 남긴다(사라진 이상의 닫음 기록을 지워, 다시 생기면 다시 보이게). */
export function pruneDismissed(dismissed: readonly string[], liveKeys: readonly string[]): string[] {
  const live = new Set(liveKeys);
  return dismissed.filter((k) => live.has(k));
}
