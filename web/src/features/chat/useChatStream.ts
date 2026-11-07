import { useCallback, useRef, useState } from "react";
import { sendMessage, type Source, type Stage } from "@/api/chat";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  latencyMs?: number;
  error?: string;
};

/** 질문을 보내고 SSE 토큰을 받아 마지막 답변 메시지에 이어 붙인다. */
export function useChatStream(conversationId: string) {
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
        if (!controller.signal.aborted) {
          const message = err instanceof Error ? err.message : String(err);
          patchAssistant(assistantId, (m) => ({ ...m, error: message }));
        }
      } finally {
        setIsStreaming(false);
        setStage(null);
        abortRef.current = null;
      }
    },
    [conversationId, patchAssistant],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  return { messages, stage, isStreaming, send, stop };
}
