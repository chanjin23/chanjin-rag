import type { Coverage, Source } from "@/api/chat";
import { apiFetch } from "@/api/client";

/**
 * 대화·피드백·색인 범위 API (TRD 8.3, F-11, F-12, F-18). 백엔드 app/api/{conversations,feedback,index}.py와 맞춘다.
 */
export type Conversation = {
  id: number;
  /** 첫 질문 앞 40자. 빈 대화(`POST /conversations`)는 첫 질문 전까지 빈 문자열 */
  title: string;
  created_at: string;
  updated_at: string;
};

export type FeedbackReason = "wrong" | "not_found_wrong" | "other";

export type Feedback = { rating: 1 | -1; reason: FeedbackReason | null; note: string | null };

export type StoredMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
  status: "streaming" | "complete" | "aborted" | "error";
  /** 보낸 sources 그대로(인용 번호가 없는 참고 후보 포함) */
  citations: Source[];
  created_at: string;
  not_found: boolean;
  coverage: Coverage | null;
  feedback: Feedback | null;
};

export type ConversationDetail = Conversation & { messages: StoredMessage[] };

export const CONVERSATIONS_KEY = ["conversations"] as const;
export const conversationKey = (id: number) => ["conversations", id] as const;
export const INDEX_COVERAGE_KEY = ["index", "coverage"] as const;

export async function listConversations(): Promise<Conversation[]> {
  return (await (await apiFetch("/conversations")).json()) as Conversation[];
}

export async function getConversation(id: number): Promise<ConversationDetail> {
  return (await (await apiFetch(`/conversations/${id}`)).json()) as ConversationDetail;
}

export async function renameConversation(id: number, title: string): Promise<Conversation> {
  const res = await apiFetch(`/conversations/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
  return (await res.json()) as Conversation;
}

export async function deleteConversation(id: number): Promise<void> {
  await apiFetch(`/conversations/${id}`, { method: "DELETE" });
}

export async function saveFeedback(messageId: number, feedback: Feedback): Promise<Feedback> {
  const res = await apiFetch(`/messages/${messageId}/feedback`, {
    method: "PUT",
    body: JSON.stringify(feedback),
  });
  return (await res.json()) as Feedback;
}

/** 색인 범위(F-18): 키워드 검색 가능 수, 의미 검색 가능 수, 완료 예상일 */
export type IndexCoverage = {
  by_status: Record<string, number>;
  total_docs: number;
  bm25_docs: number;
  vector_docs: number;
  pending_docs: number;
  embed_waiting: number;
  daily_limit: number;
  used_today: number;
  /** YYYY-MM-DD(한국 시간). 기다리는 파일이 없으면 null */
  estimated_completion: string | null;
};

export async function getIndexCoverage(): Promise<IndexCoverage> {
  return (await (await apiFetch("/index/coverage")).json()) as IndexCoverage;
}
