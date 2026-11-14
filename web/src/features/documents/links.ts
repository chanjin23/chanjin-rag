import type { Source } from "@/api/chat";

/** 문서 상세로 넘기는 화면 상태: 저장된 인용(삭제·변경된 문서면 이 값으로 구절을 보여준다) */
export type DetailState = { citation?: Source } | null;

/**
 * 출처 → 문서 상세 주소(TRD 9.1). 인용 위치는 `?loc=<위치 JSON>`으로 넘겨 상세에서 스크롤·강조한다.
 * 요약 출처처럼 위치가 없으면 `?loc=` 없이 연다.
 */
export function documentDetailHref(source: Pick<Source, "document_id" | "location">): string {
  const base = `/documents/${source.document_id}`;
  const loc = source.location ?? {};
  return Object.keys(loc).length ? `${base}?loc=${encodeURIComponent(JSON.stringify(loc))}` : base;
}
