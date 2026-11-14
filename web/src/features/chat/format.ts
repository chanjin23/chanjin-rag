/** 채팅 화면 표시 도우미: 파일명, 위치. 시각 표시는 공용 `@/lib/time`. */

const LOCATION_LABEL: Record<string, string> = {
  page: "쪽",
  slide: "슬라이드",
  paragraph: "문단",
  table: "표",
  row: "행",
  sheet: "시트",
};

export function fileName(path: string): string {
  return path.split(/[\\/]/).pop() ?? path;
}

export function formatLocation(location: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, label] of Object.entries(LOCATION_LABEL)) {
    if (location[key] === undefined) continue;
    const end = location[`${key}_end`];
    parts.push(`${label} ${String(location[key])}${end !== undefined ? `-${String(end)}` : ""}`);
  }
  const lines = location.lines;
  if (Array.isArray(lines) && lines.length === 2) parts.push(`${lines[0]}-${lines[1]}줄`);
  return parts.join(", ");
}

export function isWindows(): boolean {
  const data = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData;
  return (data?.platform ?? navigator.userAgent).includes("Win");
}

/**
 * "폴더에서 보기" 주소(TRD 10.2): `chanjin-sync://reveal?k=<path_key의 SHA-256>`.
 * path_key는 백엔드 `make_path_key`와 같은 식(NFC → 소문자 → NFC). 해시를 못 구하는 환경이면 null.
 */
export async function revealUrl(path: string): Promise<string | null> {
  if (!globalThis.crypto?.subtle) return null;
  const key = path.normalize("NFC").toLowerCase().normalize("NFC");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key));
  const hex = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
  return `chanjin-sync://reveal?k=${hex}`;
}
