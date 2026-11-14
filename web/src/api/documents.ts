import { apiFetch } from "@/api/client";

/**
 * 문서 API (TRD 8.3, F-05, F-10, F-19). 백엔드 app/api/documents.py와 맞춘다.
 */
export type DocumentStatus =
  | "queued"
  | "meta_only"
  | "extracting"
  | "indexed"
  | "no_text"
  | "embedding"
  | "ready"
  | "failed";

export type FailureCode =
  | "encrypted"
  | "corrupted"
  | "timeout"
  | "memory"
  | "hash_mismatch"
  | "parser_error"
  | "unsupported_format";

export type DocumentItem = {
  id: number;
  path: string;
  filename: string;
  ext: string;
  size: number;
  mtime: string;
  status: DocumentStatus;
  failure_code: FailureCode | null;
  failure_detail: string | null;
  locally_deleted: boolean;
  deleted_at: string | null;
  /** 재처리 요청됨: 다음 동기화 때 Agent가 다시 올린다 */
  needs_upload: boolean;
  chunks: number;
  updated_at: string;
};

export type DocumentList = {
  items: DocumentItem[];
  total: number;
  page: number;
  page_size: number;
  /** 상태별 개수 + `locally_deleted` (필터와 무관한 전체 기준) */
  counts: Record<DocumentStatus | "locally_deleted", number>;
};

export type DocumentSection = {
  seq: number;
  location: Record<string, unknown>;
  heading_path: string[];
  kind: "text" | "table" | "slide";
  text: string;
};

export type DocumentDetail = DocumentItem & { sections: DocumentSection[] };

export type ReprocessResult = {
  id: number;
  action: "needs_upload" | "rechunk";
  document: DocumentItem;
};

export type DocumentQuery = {
  status?: DocumentStatus[];
  locallyDeleted?: boolean;
  ext?: string[];
  path?: string;
  q?: string;
  page?: number;
  pageSize?: number;
};

export const DOCUMENTS_KEY = ["documents"] as const;
export const documentKey = (id: number) => ["documents", "detail", id] as const;

export async function listDocuments(query: DocumentQuery): Promise<DocumentList> {
  const params = new URLSearchParams();
  for (const s of query.status ?? []) params.append("status", s);
  for (const e of query.ext ?? []) params.append("ext", e);
  if (query.locallyDeleted !== undefined) params.set("locally_deleted", String(query.locallyDeleted));
  if (query.path?.trim()) params.set("path", query.path.trim());
  if (query.q?.trim()) params.set("q", query.q.trim());
  if (query.page) params.set("page", String(query.page));
  if (query.pageSize) params.set("page_size", String(query.pageSize));
  const qs = params.toString();
  return (await (await apiFetch(`/documents${qs ? `?${qs}` : ""}`)).json()) as DocumentList;
}

export async function getDocument(id: number): Promise<DocumentDetail> {
  return (await (await apiFetch(`/documents/${id}`)).json()) as DocumentDetail;
}

export async function reprocessDocument(id: number): Promise<ReprocessResult> {
  return (await (await apiFetch(`/documents/${id}/reprocess`, { method: "POST" })).json()) as ReprocessResult;
}

export async function purgeDocument(id: number): Promise<void> {
  await apiFetch(`/documents/${id}`, { method: "DELETE" });
}

/** 경로 앞부분 → 한 단계 아래 폴더 후보(채팅 디렉터리 필터 자동완성) */
export async function suggestFolders(prefix: string): Promise<string[]> {
  return (await (await apiFetch(`/documents/suggest?prefix=${encodeURIComponent(prefix)}`)).json()) as string[];
}
