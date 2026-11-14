import { type UseQueryResult, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { ME_QUERY_KEY } from "@/api/auth";
import { type Coverage, NEW_CONVERSATION, type Source, type Stage } from "@/api/chat";
import {
  CONVERSATIONS_KEY,
  conversationKey,
  getConversation,
  getIndexCoverage,
  INDEX_COVERAGE_KEY,
  listConversations,
} from "@/api/conversations";
import { loginPath } from "@/features/auth/redirect";
import { NARROW_QUERY, useMediaQuery } from "@/hooks/useMediaQuery";
import { cn } from "@/lib/cn";
import { AnswerMarkdown } from "./AnswerMarkdown";
import {
  activeFilterCount,
  ChatFiltersForm,
  EMPTY_FILTERS,
  type FilterState,
  toApiFilters,
} from "./ChatFilters";
import { ConversationList, displayTitle } from "./ConversationList";
import { clearDraft, loadDraft, saveDraft } from "./draft";
import { FeedbackBar } from "./FeedbackBar";
import { formatMonthDay } from "@/lib/time";
import { HEALTH_KEY } from "@/api/health";
import { getSetupStatus, SETUP_STATUS_KEY, type SetupStatus } from "@/api/setup";
import { SetupChecklist } from "@/features/setup/SetupChecklist";
import { fileName } from "./format";
import { type PanelFocus, SourcePanel } from "./SourcePanel";
import { type ChatMessage, fromStored, useChatStream } from "./useChatStream";

/**
 * 채팅 화면 (TRD 9.1, 9.2): 왼쪽 대화 목록, 가운데 메시지·필터·입력창(위에 색인 범위 한 줄),
 * 오른쪽 출처 패널(접기). 너비 768px 미만에서는 대화 목록과 출처 패널을 서랍으로 연다.
 */

const STAGE_LABEL: Record<Stage, string> = {
  rewriting: "질문 정리 중",
  searching: "문서 검색 중",
  reranking: "결과 재정렬 중",
  grading: "근거 확인 중",
  retrying: "다시 검색 중",
  summarizing: "문서 요약 중",
  generating: "답변 작성 중",
};

const nf = new Intl.NumberFormat("ko-KR");

/** 문서 상세의 "이 문서 요약"이 넘기는 요청(F-09): 새 대화에서 한 번 보낸다 */
export type SummarizeRequest = { documentId: number; filename: string };
type RouteState = { createdFrom?: string; summarize?: SummarizeRequest } | null;
/** 이미 보낸 요약 요청(화면 history 항목 key). StrictMode 재실행·뒤로 가기로 다시 보내지 않게 한다 */
const sentSummaries = new Set<string>();

/** 찾지 못함 응답의 검색 범위(TRD 7.1 coverage). */
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

/** 입력창 위 색인 범위 한 줄(F-18). 예: "키워드 검색 1,000 · 의미 검색 340 · 완료 예상 11월 14일" */
function IndexCoverageLine() {
  const coverage = useQuery({ queryKey: INDEX_COVERAGE_KEY, queryFn: getIndexCoverage });
  if (!coverage.data) return null;
  const c = coverage.data;
  return (
    <p className="mb-1.5 text-xs text-slate-500" data-testid="index-coverage">
      키워드 검색 {nf.format(c.bm25_docs)} · 의미 검색 {nf.format(c.vector_docs)} ·{" "}
      {c.estimated_completion
        ? `완료 예상 ${formatMonthDay(c.estimated_completion)}`
        : "임베딩 완료"}
    </p>
  );
}

function Drawer({
  side,
  label,
  open,
  onClose,
  children,
}: {
  side: "left" | "right";
  label: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: globalThis.KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40">
      <button
        type="button"
        aria-label={`${label} 닫기`}
        onClick={onClose}
        className="absolute inset-0 bg-slate-900/40"
      />
      <div
        role="dialog"
        aria-label={label}
        className={cn(
          "absolute top-0 flex h-full w-[85%] max-w-sm flex-col bg-slate-50 shadow-xl",
          side === "left" ? "left-0" : "right-0",
        )}
      >
        {children}
      </div>
    </div>
  );
}

function AssistantBody({
  m,
  onCite,
  onRetry,
  onShowSources,
  onFeedback,
  canRetry,
}: {
  m: ChatMessage;
  onCite: (n: number) => void;
  onRetry: () => void;
  onShowSources: () => void;
  onFeedback: (f: NonNullable<ChatMessage["feedback"]>) => void;
  canRetry: boolean;
}) {
  const numbers = (m.sources ?? []).flatMap((s) => (s.n !== null ? [s.n] : []));
  const others = (m.sources ?? []).filter((s) => s.n === null);
  const done = m.messageId !== undefined && !m.error;
  return (
    <>
      {m.content ? (
        <AnswerMarkdown content={m.content} citationNumbers={numbers} onCite={onCite} />
      ) : (
        !m.error && <span className="text-slate-400">…</span>
      )}
      {m.notFound && m.coverage && <CoverageLine coverage={m.coverage} />}
      {m.notFound && others.length > 0 && (
        <p className="mt-1 text-xs text-slate-500">
          참고 후보:{" "}
          <button type="button" onClick={onShowSources} className="underline hover:text-slate-800">
            {others.map((s) => fileName(s.path)).join(", ")}
          </button>
        </p>
      )}
      {m.error && (
        <div className="mt-1 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700" data-testid="chat-error">
          <p>{m.error.message}</p>
          {m.error.retryable && (
            <button
              type="button"
              onClick={onRetry}
              disabled={!canRetry}
              className="mt-1.5 rounded border border-red-300 bg-white px-2 py-0.5 text-xs text-red-700 hover:bg-red-100 disabled:opacity-40"
            >
              다시 시도
            </button>
          )}
        </div>
      )}
      {m.latencyMs !== undefined && <p className="mt-1 text-xs text-slate-400">{m.latencyMs}ms</p>}
      {done && m.messageId !== undefined && (
        <FeedbackBar messageId={m.messageId} feedback={m.feedback} onSaved={onFeedback} />
      )}
    </>
  );
}

export function ChatPage() {
  const { conversationId = NEW_CONVERSATION } = useParams();
  const location = useLocation();
  // 새 대화가 만들어져 URL이 `/c/{id}`로 바뀔 때는 같은 화면을 유지한다(스트리밍 중인 답변을 잃지 않게)
  // 새 대화 화면은 열 때마다 새로 시작한다("새 대화"를 다시 눌러도 비운다)
  const viewKey =
    (location.state as RouteState)?.createdFrom ??
    (conversationId === NEW_CONVERSATION ? `new-${location.key}` : conversationId);
  return <ChatView key={viewKey} viewKey={viewKey} conversationId={conversationId} />;
}

/**
 * 메시지가 없을 때: 문서가 0개면 첫 실행 체크리스트(F-18), 아니면 질문 안내.
 * 체크리스트 상태를 읽는 중이면 로딩 표시, 읽지 못하면 질문 안내와 함께 확인 실패를 알린다.
 */
function EmptyState({ setup }: { setup: UseQueryResult<SetupStatus> }) {
  if (setup.isPending) {
    return (
      <p className="pt-20 text-center text-sm text-slate-400" data-testid="empty-loading">
        불러오는 중…
      </p>
    );
  }
  if (setup.data?.documents === 0) return <SetupChecklist status={setup.data} />;
  return (
    <div className="pt-20 text-center text-sm text-slate-500">
      <p>내 문서에 대해 질문해 보세요. 답변에는 출처가 함께 표시됩니다.</p>
      {setup.isError && (
        <p className="mt-2 text-xs text-slate-400" data-testid="setup-status-error">
          첫 실행 상태를 확인하지 못했습니다. 잠시 후 새로 고쳐 보세요.
        </p>
      )}
    </div>
  );
}

function ChatView({ conversationId, viewKey }: { conversationId: string; viewKey: string }) {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const narrow = useMediaQuery(NARROW_QUERY);
  const mountId = useRef(conversationId).current;
  const numericId = /^\d+$/.test(conversationId) ? Number(conversationId) : null;

  const [listOpen, setListOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(true);
  const [panelDrawer, setPanelDrawer] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [filters, setFilters] = useState<FilterState>(EMPTY_FILTERS);
  const [selected, setSelected] = useState<string | null>(null);
  const [focus, setFocus] = useState<PanelFocus | null>(null);

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
  const onConversation = useCallback(
    (id: number) => {
      navigate(`/c/${id}`, { replace: true, state: { createdFrom: viewKey } });
      void queryClient.invalidateQueries({ queryKey: CONVERSATIONS_KEY, exact: true });
    },
    [viewKey, navigate, queryClient],
  );
  const onSettled = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: CONVERSATIONS_KEY, exact: true });
    void queryClient.invalidateQueries({ queryKey: INDEX_COVERAGE_KEY });
    // 모델 오류(특히 provider_auth)를 전역 배너에 바로 보이게 상태 점검을 다시 읽는다(TRD 9.1)
    void queryClient.invalidateQueries({ queryKey: HEALTH_KEY });
    void queryClient.invalidateQueries({ queryKey: SETUP_STATUS_KEY });
  }, [queryClient]);

  const { messages, setMessages, stage, progress, isStreaming, send, retry, stop, setFeedback } =
    useChatStream(conversationId, { onUnauthenticated, onConversation, onSettled });
  const [input, setInput] = useState(() => loadDraft(conversationId));

  // 기존 대화로 들어오면 저장된 메시지를 불러온다(새 대화에서 이어진 화면은 이미 메시지가 있다)
  const loadFromServer = mountId === conversationId && numericId !== null;
  const detail = useQuery({
    queryKey: conversationKey(numericId ?? 0),
    queryFn: () => getConversation(numericId!),
    enabled: loadFromServer,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });
  const conversations = useQuery({ queryKey: CONVERSATIONS_KEY, queryFn: listConversations });
  // 문서가 0개면 빈 안내 대신 첫 실행 체크리스트(F-18, TRD 9.1)
  const setup = useQuery({ queryKey: SETUP_STATUS_KEY, queryFn: getSetupStatus, retry: 1 });
  const listed = conversations.data?.find((c) => c.id === numericId);
  const loaded = useRef(false);
  useEffect(() => {
    if (!detail.data || loaded.current) return;
    loaded.current = true;
    setMessages(detail.data.messages.map(fromStored));
  }, [detail.data, setMessages]);

  // 문서 상세에서 "이 문서 요약"으로 들어오면 그 문서를 요약해 달라고 보낸다(summarize_document_id)
  useEffect(() => {
    const request = (location.state as RouteState)?.summarize;
    if (!request || sentSummaries.has(location.key)) return;
    sentSummaries.add(location.key);
    void send(`「${request.filename}」 요약해줘`, { summarizeDocumentId: request.documentId });
  }, [location.key, location.state, send]);

  const assistants = messages.filter((m) => m.role === "assistant");
  const current =
    assistants.find((m) => m.id === selected) ??
    [...assistants].reverse().find((m) => (m.sources?.length ?? 0) > 0) ??
    null;

  const openSources = (id: string, n?: number) => {
    setSelected(id);
    if (n !== undefined) setFocus({ n, nonce: Date.now() });
    if (narrow) setPanelDrawer(true);
    else setPanelOpen(true);
  };

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const text = input.trim();
    if (!text || isStreaming) return;
    setInput("");
    clearDraft(conversationId);
    setSelected(null);
    setFocus(null);
    void send(text, { filters: toApiFilters(filters) });
  };

  const summarize = (source: Source) => {
    if (isStreaming) return;
    setSelected(null);
    setFocus(null);
    void send(`「${fileName(source.path)}」 요약해줘`, {
      summarizeDocumentId: source.document_id,
      filters: toApiFilters(filters),
    });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const filterCount = activeFilterCount(filters);
  const notFoundSelected = current?.notFound ?? false;

  const panel = (
    <SourcePanel
      key={current?.id ?? "none"}
      sources={current?.sources ?? []}
      notFound={notFoundSelected}
      focus={focus}
      onSummarize={summarize}
    />
  );

  const list = (
    <ConversationList currentId={numericId} onNavigate={() => setListOpen(false)} />
  );

  return (
    <div className="flex h-full min-h-0">
      {!narrow && (
        <aside className="w-60 shrink-0 border-r border-slate-200 bg-slate-50">{list}</aside>
      )}
      {narrow && (
        <Drawer side="left" label="대화 목록 서랍" open={listOpen} onClose={() => setListOpen(false)}>
          {list}
        </Drawer>
      )}

      <div className="flex min-w-0 flex-1 flex-col px-4">
        <div className="flex items-center gap-2 border-b border-slate-200 py-2 text-sm">
          {narrow && (
            <button
              type="button"
              onClick={() => setListOpen(true)}
              className="rounded-md border border-slate-300 px-2 py-1 text-xs"
            >
              대화 목록
            </button>
          )}
          <h1 className="min-w-0 flex-1 truncate font-medium text-slate-700">
            {listed ? displayTitle(listed) : numericId === null ? "새 대화" : ""}
          </h1>
          <button
            type="button"
            onClick={() => (narrow ? setPanelDrawer(true) : setPanelOpen((v) => !v))}
            aria-expanded={narrow ? panelDrawer : panelOpen}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-100"
          >
            {narrow ? "출처 보기" : panelOpen ? "출처 패널 접기" : "출처 패널 열기"}
          </button>
        </div>

        <div
          className="mx-auto w-full max-w-3xl flex-1 space-y-4 overflow-y-auto py-6"
          role="log"
          aria-live="polite"
          aria-label="대화 내용"
        >
          {detail.isError && (
            <p className="pt-20 text-center text-sm text-slate-500">대화를 찾을 수 없습니다.</p>
          )}
          {messages.length === 0 && !detail.isError && !detail.isLoading && (
            <EmptyState setup={setup} />
          )}
          {messages.map((m) => (
            <div key={m.id} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
              <div
                data-testid={m.role === "assistant" ? "assistant-message" : "user-message"}
                className={cn(
                  "max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
                  m.role === "user"
                    ? "whitespace-pre-wrap bg-slate-900 text-white"
                    : cn(
                        "border bg-white",
                        current?.id === m.id ? "border-sky-200" : "border-slate-200",
                      ),
                )}
              >
                {m.role === "assistant" ? (
                  <AssistantBody
                    m={m}
                    onCite={(n) => openSources(m.id, n)}
                    onShowSources={() => openSources(m.id)}
                    onRetry={() => retry(m.id)}
                    canRetry={!isStreaming}
                    onFeedback={(f) => setFeedback(m.id, f)}
                  />
                ) : (
                  m.content
                )}
              </div>
            </div>
          ))}
        </div>

        <form onSubmit={submit} className="mx-auto w-full max-w-3xl border-t border-slate-200 py-3">
          {stage && (
            <p className="mb-2 text-xs text-slate-500" role="status">
              {STAGE_LABEL[stage]}
              {progress && ` (${progress.done}/${progress.total})`}…
            </p>
          )}
          {filtersOpen && <ChatFiltersForm value={filters} onChange={setFilters} />}
          <div className="flex items-center gap-2">
            <IndexCoverageLine />
            <button
              type="button"
              onClick={() => setFiltersOpen((v) => !v)}
              aria-expanded={filtersOpen}
              className="mb-1.5 ml-auto rounded px-2 py-0.5 text-xs text-slate-500 hover:bg-slate-100"
            >
              필터{filterCount > 0 ? ` (${filterCount})` : ""}
            </button>
          </div>
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

      {!narrow && panelOpen && (
        <aside
          aria-label="출처 패널"
          className="w-80 shrink-0 overflow-y-auto border-l border-slate-200 bg-slate-50 p-3"
        >
          {panel}
        </aside>
      )}
      {narrow && (
        <Drawer side="right" label="출처 패널" open={panelDrawer} onClose={() => setPanelDrawer(false)}>
          <div className="overflow-y-auto p-3">{panel}</div>
        </Drawer>
      )}
    </div>
  );
}
