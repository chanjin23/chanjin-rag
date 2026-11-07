import { apiFetch } from "@/api/client";
import { readSse } from "@/api/sse";

/** SSE 이벤트 타입 (TRD 7.3). 백엔드 app/api/chat.py와 맞춘다. */
export type Stage = "rewriting" | "searching" | "reranking" | "grading" | "retrying" | "generating";

export type Source = {
  n: number;
  chunk_id: number;
  path: string;
  location: Record<string, unknown>;
  snippet: string;
  locally_deleted: boolean;
};

export type ChatEvent =
  | { type: "status"; stage: Stage }
  | { type: "sources"; sources: Source[] }
  | { type: "token"; text: string }
  | { type: "done"; message_id: number | null; not_found: boolean; latency_ms: number }
  | { type: "error"; code: string; message: string };

export type MessageIn = { text: string };

export async function* sendMessage(
  conversationId: string,
  body: MessageIn,
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const res = await apiFetch(`/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: "POST",
    body: JSON.stringify(body),
    signal,
  });

  for await (const { event, data } of readSse(res, signal)) {
    const payload: unknown = JSON.parse(data);
    switch (event) {
      case "status":
        yield { type: "status", ...(payload as { stage: Stage }) };
        break;
      case "sources":
        yield { type: "sources", sources: payload as Source[] };
        break;
      case "token":
        yield { type: "token", ...(payload as { text: string }) };
        break;
      case "done":
        yield { type: "done", ...(payload as Omit<Extract<ChatEvent, { type: "done" }>, "type">) };
        break;
      case "error":
        yield { type: "error", ...(payload as { code: string; message: string }) };
        break;
    }
  }
}
