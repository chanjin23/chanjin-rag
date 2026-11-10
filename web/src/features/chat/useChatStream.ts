import { useCallback, useRef, useState } from "react";
import {
  type Coverage,
  type Progress,
  type Source,
  type Stage,
  sendMessage,
} from "@/api/chat";
import { ApiError, isUnauthenticated } from "@/api/client";

export type ChatError = { code: string; message: string; retryable: boolean };

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** 백엔드 메시지 ID(done 이후) */
  messageId?: number;
  sources?: Source[];
  notFound?: boolean;
  coverage?: Coverage;
  latencyMs?: number;
  error?: ChatError;
};

type Options = {
  /** 세션이 없거나 만료돼 401을 받았을 때. 보내지 못한 질문을 넘긴다(입력 보존용). */
  onUnauthenticated?: (text: string) => void;
};

/**
 * 질문을 보내고 SSE 이벤트를 받아 마지막 답변 메시지를 채운다(TRD 7.3).
 * 첫 질문은 새 대화(`new`)로 보내고, done의 conversation_id를 기억해 다음 질문부터 같은 대화로 보낸다.
 * 대화 목록과 URL 이동은 T11에서 붙인다.
 */
export function useChatStream(conversationId: string, { onUnauthenticated }: Options = {}) {
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
    async (text: string) => {
      const userId = crypto.randomUUID();
      const assistantId = crypto.randomUUID();
      setMessages((prev) => [
        ...prev,
        { id: userId, role: "user", content: text },
        { id: assistantId, role: "assistant", content: "" },
      ]);

      const controller = new AbortController();
      abortRef.current = controller;
      setIsStreaming(true);

      try {
        for await (const ev of sendMessage(conversationRef.current, { text }, controller.signal)) {
          if (ev.type === "status") {
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
                  message: err instanceof Error ? err.message : String(err),
                  retryable: true,
                };
          patchAssistant(assistantId, (m) => ({ ...m, error }));
        }
      } finally {
        setIsStreaming(false);
        setStage(null);
        setProgress(null);
        abortRef.current = null;
      }
    },
    [patchAssistant, onUnauthenticated],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  return { messages, stage, progress, isStreaming, send, stop };
}
