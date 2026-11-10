import { apiFetch } from "@/api/client";
import { readSse } from "@/api/sse";

/**
 * 질문 API와 SSE 이벤트 타입 (TRD 7.3, 8.3). 백엔드 app/api/chat.py와 맞춘다.
 *
 * 경우별 순서
 * - 일반 답변: status(여러 번) → sources → token(여러 번) → done(not_found=false)
 * - 찾지 못함: status(여러 번) → sources(가까운 후보 3개, n=null) → token(고정 문구) → done(not_found=true)
 * - 필터 결과 0개·문서 0개: token(안내 문구) → done(not_found=true)
 * - 요약: status(summarizing, progress)(여러 번) → sources(대상 문서) → token → done
 * - 요약 대상 여러 개: sources(후보 문서들, n=null) → token(고르라는 안내) → done
 * - 실패: 어느 시점에서든 error로 끝남
 */
export type Stage =
  | "rewriting"
  | "searching"
  | "reranking"
  | "grading"
  | "retrying"
  | "summarizing"
  | "generating";

export type Progress = { done: number; total: number };

export type Source = {
  /** 인용 번호. 찾지 못함의 참고 후보·요약 후보 목록은 null(인용 번호 없음) */
  n: number | null;
  chunk_id: number | null;
  document_id: number;
  path: string;
  location: Record<string, unknown>;
  /** 문서 수정 시각(ISO 8601) */
  mtime: string;
  snippet: string;
  locally_deleted: boolean;
};

/** 질의 시점의 검색 범위(TRD 7.1) */
export type Coverage = {
  bm25_docs: number;
  vector_docs: number;
  pending_docs: number;
  total_docs: number;
  filters_applied: Record<string, unknown>;
};

export type ErrorCode =
  | "rate_limited"
  | "provider_unavailable"
  | "provider_auth"
  | "timeout"
  | "conflict"
  | "internal";

export type DoneEvent = {
  message_id: number;
  conversation_id: number;
  not_found: boolean;
  coverage: Coverage;
  latency_ms: number;
};

export type ChatEvent =
  | { type: "status"; stage: Stage; progress?: Progress }
  | { type: "sources"; sources: Source[] }
  | { type: "token"; text: string }
  | ({ type: "done" } & DoneEvent)
  | { type: "error"; code: ErrorCode | string; message: string; retryable: boolean };

export type ChatFilters = {
  directory?: string;
  exts?: string[];
  modified_from?: string;
  modified_to?: string;
  include_deleted?: boolean;
};

export type MessageIn = { text: string; filters?: ChatFilters; summarize_document_id?: number };

/** 새 대화로 보낼 때의 경로 값. 백엔드가 대화를 만들고 done에 conversation_id를 돌려준다. */
export const NEW_CONVERSATION = "new";

export async function* sendMessage(
  conversationId: string,
  body: MessageIn,
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  // 같은 대화에서 생성 중이면 SSE 대신 409(conflict)가 오고 apiFetch가 ApiError로 던진다
  const res = await apiFetch(`/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: "POST",
    body: JSON.stringify(body),
    signal,
  });

  for await (const { event, data } of readSse(res, signal)) {
    const payload: unknown = JSON.parse(data);
    switch (event) {
      case "status":
        yield { type: "status", ...(payload as { stage: Stage; progress?: Progress }) };
        break;
      case "sources":
        yield { type: "sources", sources: payload as Source[] };
        break;
      case "token":
        yield { type: "token", ...(payload as { text: string }) };
        break;
      case "done":
        yield { type: "done", ...(payload as DoneEvent) };
        break;
      case "error":
        yield {
          type: "error",
          ...(payload as { code: string; message: string; retryable: boolean }),
        };
        break;
    }
  }
}
