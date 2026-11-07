import { useCallback, useRef, useState } from "react";
import { sendMessage, type Source, type Stage } from "@/api/chat";
import { isUnauthenticated } from "@/api/client";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  latencyMs?: number;
  error?: string;
};

type Options = {
  /** 세션이 없거나 만료돼 401을 받았을 때. 보내지 못한 질문을 넘긴다(입력 보존용). */
  onUnauthenticated?: (text: string) => void;
};

/** 질문을 보내고 SSE 토큰을 받아 마지막 답변 메시지에 이어 붙인다. */
export function useChatStream(conversationId: string, { onUnauthenticated }: Options = {}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [stage, setStage] = useState<Stage | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

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
        for await (const ev of sendMessage(conversationId, { text }, controller.signal)) {
          if (ev.type === "status") setStage(ev.stage);
          else if (ev.type === "sources") patchAssistant(assistantId, (m) => ({ ...m, sources: ev.sources }));
          else if (ev.type === "token") patchAssistant(assistantId, (m) => ({ ...m, content: m.content + ev.text }));
          else if (ev.type === "done") patchAssistant(assistantId, (m) => ({ ...m, latencyMs: ev.latency_ms }));
          else if (ev.type === "error") patchAssistant(assistantId, (m) => ({ ...m, error: ev.message }));
        }
      } catch (err) {
        if (isUnauthenticated(err)) {
          // 보내지 못한 질문 말풍선은 지우고, 질문은 호출한 쪽이 보존한다
          setMessages((prev) => prev.filter((m) => m.id !== userId && m.id !== assistantId));
          onUnauthenticated?.(text);
        } else if (!controller.signal.aborted) {
          const message = err instanceof Error ? err.message : String(err);
          patchAssistant(assistantId, (m) => ({ ...m, error: message }));
        }
      } finally {
        setIsStreaming(false);
        setStage(null);
        abortRef.current = null;
      }
    },
    [conversationId, patchAssistant, onUnauthenticated],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  return { messages, stage, isStreaming, send, stop };
}
