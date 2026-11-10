import { useCallback, useRef, useState } from "react";
import {
  type ChatFilters,
  type Coverage,
  type Progress,
  type Source,
  type Stage,
  sendMessage,
} from "@/api/chat";
import { ApiError, isUnauthenticated } from "@/api/client";
import type { Feedback, StoredMessage } from "@/api/conversations";

export type ChatError = { code: string; message: string; retryable: boolean };

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** 백엔드 메시지 ID(done 이후, 또는 저장된 메시지) */
  messageId?: number;
  sources?: Source[];
  notFound?: boolean;
  coverage?: Coverage;
  latencyMs?: number;
  error?: ChatError;
  feedback?: Feedback | null;
  /** 다시 시도할 때 같은 조건으로 보내기 위한 원래 요청 */
  request?: SendOptions & { text: string };
};

export type SendOptions = { filters?: ChatFilters; summarizeDocumentId?: number };

type Options = {
  /** 세션이 없거나 만료돼 401을 받았을 때. 보내지 못한 질문을 넘긴다(입력 보존용). */
  onUnauthenticated?: (text: string) => void;
  /** 응답 헤더로 대화 ID를 알게 됐을 때(새 대화면 이때 URL을 바꾼다) */
  onConversation?: (conversationId: number) => void;
  /** 답변이 끝났을 때(done·error). 대화 목록·색인 범위를 다시 읽는 데 쓴다. */
  onSettled?: () => void;
};

/** 저장된 메시지 → 화면 메시지. 오류·중단으로 끝난 답변은 다시 보낼 수 없어 안내만 한다. */
export function fromStored(m: StoredMessage): ChatMessage {
  const base: ChatMessage = {
    id: `m${m.id}`,
    role: m.role,
    content: m.content,
    messageId: m.id,
  };
  if (m.role === "user") return base;
  return {
    ...base,
    sources: m.citations,
    notFound: m.not_found,
    coverage: m.coverage ?? undefined,
    feedback: m.feedback,
    error:
      m.status === "error"
        ? { code: "stored_error", message: "답변을 끝내지 못했습니다", retryable: false }
        : m.status === "aborted"
          ? { code: "aborted", message: "답변을 중지했습니다", retryable: false }
          : undefined,
  };
}

/**
 * 질문을 보내고 SSE 이벤트를 받아 마지막 답변 메시지를 채운다(TRD 7.3).
 * 새 대화는 `new`로 보내고, 응답 헤더(`conversation` 이벤트)의 대화 ID를 기억해 다음 질문부터 같은 대화로 보낸다.
 */
export function useChatStream(
  conversationId: string,
  { onUnauthenticated, onConversation, onSettled }: Options = {},
) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [stage, setStage] = useState<Stage | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const conversationRef = useRef(conversationId);

  const patchAssistant = useCallback((id: string, patch: (m: ChatMessage) => ChatMessage) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? patch(m) : m)));
  }, []);

  const send = useCallback(
    async (text: string, options: SendOptions = {}) => {
      const userId = crypto.randomUUID();
      const assistantId = crypto.randomUUID();
      const request = { text, ...options };
      setMessages((prev) => [
        ...prev,
        { id: userId, role: "user", content: text },
        { id: assistantId, role: "assistant", content: "", request },
      ]);

      const controller = new AbortController();
      abortRef.current = controller;
      setIsStreaming(true);

      const body = {
        text,
        ...(options.filters ? { filters: options.filters } : {}),
        ...(options.summarizeDocumentId !== undefined
          ? { summarize_document_id: options.summarizeDocumentId }
          : {}),
      };
      try {
        for await (const ev of sendMessage(conversationRef.current, body, controller.signal)) {
          if (ev.type === "conversation") {
            if (conversationRef.current !== String(ev.conversation_id)) {
              conversationRef.current = String(ev.conversation_id);
              onConversation?.(ev.conversation_id);
            }
          } else if (ev.type === "status") {
            setStage(ev.stage);
            setProgress(ev.progress ?? null);
          } else if (ev.type === "sources") {
            patchAssistant(assistantId, (m) => ({ ...m, sources: ev.sources }));
          } else if (ev.type === "token") {
            patchAssistant(assistantId, (m) => ({ ...m, content: m.content + ev.text }));
          } else if (ev.type === "done") {
            conversationRef.current = String(ev.conversation_id);
            patchAssistant(assistantId, (m) => ({
              ...m,
              content: m.content.trim(),
              messageId: ev.message_id,
              notFound: ev.not_found,
              coverage: ev.coverage,
              latencyMs: ev.latency_ms,
            }));
          } else if (ev.type === "error") {
            const { code, message, retryable } = ev;
            patchAssistant(assistantId, (m) => ({ ...m, error: { code, message, retryable } }));
          }
        }
      } catch (err) {
        if (isUnauthenticated(err)) {
          // 보내지 못한 질문 말풍선은 지우고, 질문은 호출한 쪽이 보존한다
          setMessages((prev) => prev.filter((m) => m.id !== userId && m.id !== assistantId));
          onUnauthenticated?.(text);
        } else if (!controller.signal.aborted) {
          const error: ChatError =
            err instanceof ApiError
              ? {
                  code: err.code,
                  message: err.message,
                  retryable: err.detail.retryable === true,
                }
              : {
                  code: "network",
                  message: "서버에 연결하지 못했습니다",
                  retryable: true,
                };
          patchAssistant(assistantId, (m) => ({ ...m, error }));
        } else {
          patchAssistant(assistantId, (m) => ({
            ...m,
            error: { code: "aborted", message: "답변을 중지했습니다", retryable: false },
          }));
        }
      } finally {
        setIsStreaming(false);
        setStage(null);
        setProgress(null);
        abortRef.current = null;
        onSettled?.();
      }
    },
    [patchAssistant, onUnauthenticated, onConversation, onSettled],
  );

  /** 오류 말풍선의 "다시 시도": 실패한 질문·답변 말풍선을 지우고 같은 요청을 다시 보낸다. */
  const retry = useCallback(
    (assistantId: string) => {
      const target = messages.find((m) => m.id === assistantId);
      if (!target?.request || isStreaming) return;
      const index = messages.indexOf(target);
      const userBefore = messages[index - 1];
      setMessages((prev) => prev.filter((m) => m.id !== assistantId && m.id !== userBefore?.id));
      const { text, ...options } = target.request;
      void send(text, options);
    },
    [messages, isStreaming, send],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const setFeedback = useCallback(
    (id: string, feedback: Feedback) => patchAssistant(id, (m) => ({ ...m, feedback })),
    [patchAssistant],
  );

  return { messages, setMessages, stage, progress, isStreaming, send, retry, stop, setFeedback };
}
