import { type FormEvent, type KeyboardEvent, useState } from "react";
import { useParams } from "react-router";
import type { Stage } from "@/api/chat";
import { cn } from "@/lib/cn";
import { useChatStream } from "./useChatStream";

const STAGE_LABEL: Record<Stage, string> = {
  rewriting: "질문 정리 중",
  searching: "문서 검색 중",
  reranking: "결과 재정렬 중",
  grading: "근거 확인 중",
  retrying: "다시 검색 중",
  generating: "답변 작성 중",
};

export function ChatPage() {
  // 대화 저장(F-11)은 DB를 붙일 때 구현한다. 지금은 URL의 id 또는 "draft"를 쓴다.
  const { conversationId = "draft" } = useParams();
  const { messages, stage, isStreaming, send, stop } = useChatStream(conversationId);
  const [input, setInput] = useState("");

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const text = input.trim();
    if (!text || isStreaming) return;
    setInput("");
    void send(text);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col px-4">
      <div className="flex-1 space-y-4 overflow-y-auto py-6">
        {messages.length === 0 && (
          <p className="pt-20 text-center text-sm text-slate-500">
            내 문서에 대해 질문해 보세요. 답변에는 출처가 함께 표시됩니다.
          </p>
        )}
        {messages.map((m) => (
          <div key={m.id} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
            <div
              className={cn(
                "max-w-[85%] whitespace-pre-wrap rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
                m.role === "user" ? "bg-slate-900 text-white" : "border border-slate-200 bg-white",
              )}
            >
              {m.content || (m.role === "assistant" && !m.error ? "…" : "")}
              {m.error && <p className="text-red-600">오류: {m.error}</p>}
              {m.latencyMs !== undefined && (
                <p className="mt-1 text-xs text-slate-400">{m.latencyMs}ms</p>
              )}
            </div>
          </div>
        ))}
      </div>

      <form onSubmit={submit} className="border-t border-slate-200 py-4">
        {stage && <p className="mb-2 text-xs text-slate-500">{STAGE_LABEL[stage]}…</p>}
        <div className="flex items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            placeholder="질문을 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)"
            className="flex-1 resize-none rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-slate-500"
          />
          {isStreaming ? (
            <button
              type="button"
              onClick={stop}
              className="rounded-xl border border-slate-300 px-4 py-2 text-sm hover:bg-slate-100"
            >
              중지
            </button>
          ) : (
            <button
              type="submit"
              disabled={!input.trim()}
              className="rounded-xl bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-40"
            >
              전송
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
