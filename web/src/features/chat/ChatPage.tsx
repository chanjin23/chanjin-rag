import { useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type KeyboardEvent, useCallback, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { ME_QUERY_KEY } from "@/api/auth";
import { type Coverage, NEW_CONVERSATION, type Source, type Stage } from "@/api/chat";
import { loginPath } from "@/features/auth/redirect";
import { cn } from "@/lib/cn";
import { clearDraft, loadDraft, saveDraft } from "./draft";
import { type ChatMessage, useChatStream } from "./useChatStream";

const STAGE_LABEL: Record<Stage, string> = {
  rewriting: "질문 정리 중",
  searching: "문서 검색 중",
  reranking: "결과 재정렬 중",
  grading: "근거 확인 중",
  retrying: "다시 검색 중",
  summarizing: "문서 요약 중",
  generating: "답변 작성 중",
};

const LOCATION_LABEL: Record<string, string> = {
  page: "쪽",
  slide: "슬라이드",
  paragraph: "문단",
  table: "표",
  row: "행",
  sheet: "시트",
};

function fileName(path: string): string {
  return path.split(/[\\/]/).pop() ?? path;
}

function formatLocation(location: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, label] of Object.entries(LOCATION_LABEL)) {
    if (location[key] === undefined) continue;
    const end = location[`${key}_end`];
    parts.push(`${label} ${String(location[key])}${end !== undefined ? `-${String(end)}` : ""}`);
  }
  const lines = location.lines;
  if (Array.isArray(lines) && lines.length === 2) parts.push(`${lines[0]}-${lines[1]}줄`);
  return parts.join(", ");
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("ko-KR");
}

/** 출처 목록(간단 표시). 출처 패널·인용 칩은 T11에서 만든다. */
function SourceList({ title, sources }: { title: string; sources: Source[] }) {
  return (
    <div className="mt-3 border-t border-slate-100 pt-2" aria-label={title}>
      <p className="text-xs font-medium text-slate-500">{title}</p>
      <ul className="mt-1 space-y-0.5">
        {sources.map((s, i) => (
          <li key={`${s.document_id}-${s.chunk_id ?? i}`} className="text-xs text-slate-600">
            {s.n !== null && <span className="mr-1 font-medium">[{s.n}]</span>}
            <span title={s.path}>{fileName(s.path)}</span>
            {formatLocation(s.location) && <span> · {formatLocation(s.location)}</span>}
            <span> · {formatDate(s.mtime)} 수정</span>
            {s.locally_deleted && <span className="text-amber-600"> · 로컬에서 삭제됨</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 찾지 못함 응답의 검색 범위(TRD 7.1 coverage). 화면 확장(색인 범위 줄)은 T11. */
function CoverageLine({ coverage }: { coverage: Coverage }) {
  const filters = Object.keys(coverage.filters_applied);
  return (
    <p className="mt-2 text-xs text-slate-500" data-testid="coverage">
      검색 범위: 키워드 검색 {coverage.bm25_docs}개 · 의미 검색 {coverage.vector_docs}개 · 대기·처리 중{" "}
      {coverage.pending_docs}개 문서
      {filters.length > 0 && ` · 적용한 필터: ${filters.join(", ")}`}
    </p>
  );
}

function AssistantBody({ m }: { m: ChatMessage }) {
  const cited = m.sources?.filter((s) => s.n !== null) ?? [];
  const others = m.sources?.filter((s) => s.n === null) ?? [];
  return (
    <>
      {m.content || (!m.error ? "…" : "")}
      {cited.length > 0 && <SourceList title="출처" sources={cited} />}
      {others.length > 0 && (
        <SourceList title={m.notFound ? "참고 후보" : "후보 문서"} sources={others} />
      )}
      {m.notFound && m.coverage && <CoverageLine coverage={m.coverage} />}
      {m.error && <p className="text-red-600">오류: {m.error.message}</p>}
      {m.latencyMs !== undefined && <p className="mt-1 text-xs text-slate-400">{m.latencyMs}ms</p>}
    </>
  );
}

export function ChatPage() {
  // 대화 목록·불러오기(F-11)는 T11에서 붙인다. URL에 id가 없으면 새 대화로 보낸다.
  const { conversationId = NEW_CONVERSATION } = useParams();
  return <ChatView key={conversationId} conversationId={conversationId} />;
}

function ChatView({ conversationId }: { conversationId: string }) {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();

  // 세션이 만료돼 보내지 못한 질문은 보존하고 로그인 화면으로 보낸다. 로그인 후 이 화면으로
  // 돌아오면 입력창에 다시 채운다(TRD 8.3, 9.2).
  const onUnauthenticated = useCallback(
    (text: string) => {
      saveDraft(conversationId, text);
      queryClient.removeQueries({ queryKey: ME_QUERY_KEY });
      navigate(loginPath(`${location.pathname}${location.search}`, "expired"), { replace: true });
    },
    [conversationId, location.pathname, location.search, navigate, queryClient],
  );
  const { messages, stage, progress, isStreaming, send, stop } = useChatStream(conversationId, {
    onUnauthenticated,
  });
  const [input, setInput] = useState(() => loadDraft(conversationId));

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const text = input.trim();
    if (!text || isStreaming) return;
    setInput("");
    clearDraft(conversationId);
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
              {m.role === "assistant" ? <AssistantBody m={m} /> : m.content}
            </div>
          </div>
        ))}
      </div>

      <form onSubmit={submit} className="border-t border-slate-200 py-4">
        {stage && (
          <p className="mb-2 text-xs text-slate-500" role="status">
            {STAGE_LABEL[stage]}
            {progress && ` (${progress.done}/${progress.total})`}…
          </p>
        )}
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
