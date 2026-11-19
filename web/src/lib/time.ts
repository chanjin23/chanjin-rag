/**
 * 시각 표시 공용 도우미(TRD 9.1 공통 화면 상태).
 * - 모든 시각은 한국 시간(Asia/Seoul)으로 보여준다.
 * - 하루 이내는 "방금", "5분 전", "3시간 전"처럼 상대 시각, 그보다 오래되면 날짜(또는 날짜·시각).
 * 채팅·문서·동기화·평가 화면과 전역 배너, 첫 실행 체크리스트가 함께 쓴다.
 */

export const TZ = "Asia/Seoul";
const DAY_MS = 24 * 3600_000;

/** "2026. 11. 14." (한국 시간 날짜) */
export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("ko-KR", { timeZone: TZ });
}

/** "11월 14일 15:30" (한국 시간). 올해가 아니면 연도를 붙인다. */
export function formatDateTime(iso: string, now = Date.now()): string {
  const date = new Date(iso);
  const year = (d: Date) => d.toLocaleDateString("ko-KR", { timeZone: TZ, year: "numeric" });
  const sameYear = year(date) === year(new Date(now));
  return date.toLocaleString("ko-KR", {
    timeZone: TZ,
    year: sameYear ? undefined : "numeric",
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** 하루 이내 상대 시각("방금", "N분 전", "N시간 전"). 하루 넘거나 미래면 null. */
function relative(iso: string, now: number): string | null {
  const diff = now - new Date(iso).getTime();
  if (diff < 0 || diff >= DAY_MS) return null;
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return "방금";
  if (minutes < 60) return `${minutes}분 전`;
  return `${Math.floor(minutes / 60)}시간 전`;
}

/** 하루 이내는 "3시간 전", 그보다 오래되면 날짜(한국 시간). */
export function formatRelative(iso: string, now = Date.now()): string {
  return relative(iso, now) ?? formatDate(iso);
}

/** 하루 이내는 "3시간 전", 그보다 오래되면 날짜와 시각(한국 시간). 상태·장애 시각에 쓴다. */
export function formatRelativeDateTime(iso: string, now = Date.now()): string {
  return relative(iso, now) ?? formatDateTime(iso, now);
}

/** "11월 14일"(한국 시간 날짜 YYYY-MM-DD) */
export function formatMonthDay(ymd: string): string {
  const [, m, d] = ymd.split("-").map(Number);
  return `${m}월 ${d}일`;
}
