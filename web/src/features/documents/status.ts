import type { DocumentQuery, DocumentStatus, FailureCode } from "@/api/documents";

/**
 * 문서 화면 상태 표기 (TRD 5.1 화면 상태 매핑, 3.4 실패 사유 코드 표, 9.1).
 * "처리 중"은 extracting·embedding, "BM25만"은 indexed, "텍스트 없음"은 no_text, "파일명만"은 meta_only.
 */
export type TabKey =
  | "all"
  | "queued"
  | "processing"
  | "indexed"
  | "ready"
  | "no_text"
  | "failed"
  | "meta_only"
  | "locally_deleted";

export type Tab = { key: TabKey; label: string; query: Pick<DocumentQuery, "status" | "locallyDeleted"> };

export const TABS: Tab[] = [
  { key: "all", label: "전체", query: {} },
  { key: "queued", label: "대기", query: { status: ["queued"] } },
  { key: "processing", label: "처리 중", query: { status: ["extracting", "embedding"] } },
  { key: "indexed", label: "BM25만", query: { status: ["indexed"] } },
  { key: "ready", label: "완료", query: { status: ["ready"] } },
  { key: "no_text", label: "텍스트 없음", query: { status: ["no_text"] } },
  { key: "failed", label: "실패", query: { status: ["failed"] } },
  { key: "meta_only", label: "파일명만", query: { status: ["meta_only"] } },
  { key: "locally_deleted", label: "로컬 삭제", query: { locallyDeleted: true } },
];

export function tabCount(tab: Tab, counts: Record<string, number> | undefined): number | null {
  if (!counts) return null;
  if (tab.key === "all") {
    return (Object.keys(STATUS_LABEL) as DocumentStatus[]).reduce((sum, s) => sum + (counts[s] ?? 0), 0);
  }
  if (tab.query.locallyDeleted) return counts.locally_deleted ?? 0;
  return (tab.query.status ?? []).reduce((sum, s) => sum + (counts[s] ?? 0), 0);
}

export const STATUS_LABEL: Record<DocumentStatus, string> = {
  queued: "대기",
  extracting: "처리 중",
  embedding: "처리 중",
  indexed: "BM25만",
  ready: "완료",
  no_text: "텍스트 없음",
  failed: "실패",
  meta_only: "파일명만",
};

export const STATUS_STYLE: Record<DocumentStatus, string> = {
  queued: "bg-slate-100 text-slate-700",
  extracting: "bg-sky-100 text-sky-800",
  embedding: "bg-sky-100 text-sky-800",
  indexed: "bg-indigo-100 text-indigo-800",
  ready: "bg-emerald-100 text-emerald-800",
  no_text: "bg-amber-100 text-amber-800",
  failed: "bg-red-100 text-red-800",
  meta_only: "bg-slate-200 text-slate-700",
};

/** TRD 3.4 실패 사유 코드 표: 의미와 화면 안내 */
export const FAILURE_INFO: Record<FailureCode, { reason: string; hint: string }> = {
  encrypted: { reason: "암호가 걸린 파일", hint: "암호를 풀고 저장하면 다음 동기화 때 다시 처리됩니다" },
  corrupted: { reason: "파일 손상, 형식 불일치", hint: "파일을 다시 저장해 보세요" },
  timeout: { reason: "파싱 60초 초과", hint: "재처리하거나 파일을 나눠 보세요" },
  memory: { reason: "메모리 상한 초과", hint: "재처리하거나 파일을 나눠 보세요" },
  hash_mismatch: { reason: "업로드 중 파일이 바뀜", hint: "다음 동기화 때 자동으로 다시 올라옵니다" },
  parser_error: { reason: "그 밖의 파서 오류", hint: "재처리하세요. 반복되면 파서 문제입니다" },
  unsupported_format: {
    reason: "아직 지원하지 않는 형식",
    hint: "이 형식은 아직 지원하지 않습니다. 후속 버전에서 지원합니다",
  },
};

/** 재처리 버튼을 보여줄 상태(백엔드: failed·no_text → 재업로드 표시, ready·indexed → 재청킹) */
export function canReprocess(status: DocumentStatus): boolean {
  return status === "failed" || status === "no_text" || status === "ready" || status === "indexed";
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 상세 링크의 `?loc=`(위치 JSON) 읽기. 잘못된 값이면 null */
export function parseLoc(raw: string | null): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isRange = (v: unknown): v is [number, number] => Array.isArray(v) && v.length === 2 && isNum(v[0]) && isNum(v[1]);

/**
 * 섹션 위치가 인용(청크) 위치 안에 드는지.
 * 청크 위치는 섹션 위치를 합친 것(TRD 6.3): 같은 키는 값이 같고, 달라지는 키는 `<키>_end`로 끝 값을 둔다.
 * `lines`는 [처음, 끝] 줄 범위가 겹치면 맞는다.
 */
export function sectionInLocation(section: Record<string, unknown>, loc: Record<string, unknown>): boolean {
  const keys = Object.keys(loc).filter((k) => !k.endsWith("_end"));
  if (!keys.length) return false;
  // 범위가 있는 첫 키(예: page..page_end)만 범위로 보고, 그 뒤의 키는 범위의 양 끝에서만 따진다
  const rangedKey = keys.find((k) => loc[`${k}_end`] !== undefined);
  for (const key of keys) {
    const want = loc[key];
    const have = section[key];
    if (key === "lines") {
      if (!isRange(want) || !isRange(have)) return false;
      if (have[1] < want[0] || have[0] > want[1]) return false;
      continue;
    }
    const end = loc[`${key}_end`];
    if (end !== undefined && isNum(want) && isNum(end) && isNum(have)) {
      if (key === rangedKey) {
        if (have < want || have > end) return false;
        continue;
      }
      // 두 번째 이후 범위 키(예: 표가 바뀌며 행 번호가 다시 시작): 값이 있으면 통과
      continue;
    }
    if (rangedKey !== undefined && key !== rangedKey && keys.indexOf(key) > keys.indexOf(rangedKey)) {
      continue;
    }
    if (JSON.stringify(have) !== JSON.stringify(want)) return false;
  }
  return true;
}
