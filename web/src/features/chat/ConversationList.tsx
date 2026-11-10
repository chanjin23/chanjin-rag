import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { NavLink, useNavigate } from "react-router";
import {
  CONVERSATIONS_KEY,
  type Conversation,
  deleteConversation,
  listConversations,
  renameConversation,
} from "@/api/conversations";
import { cn } from "@/lib/cn";
import { formatRelative } from "./format";

/** 왼쪽 대화 목록 (F-11): 새 대화, 이름 변경, 삭제. */

export const displayTitle = (c: Pick<Conversation, "title">) => c.title || "새 대화";

function Item({
  conversation,
  current,
  onNavigate,
}: {
  conversation: Conversation;
  current: boolean;
  onNavigate?: () => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"view" | "rename" | "confirmDelete">("view");
  const [title, setTitle] = useState(conversation.title);
  const [error, setError] = useState<string | null>(null);

  const rename = useMutation({
    mutationFn: (next: string) => renameConversation(conversation.id, next),
    onSuccess: async () => {
      setMode("view");
      setError(null);
      await queryClient.invalidateQueries({ queryKey: CONVERSATIONS_KEY, exact: true });
    },
    onError: () => setError("이름을 바꾸지 못했습니다"),
  });
  const remove = useMutation({
    mutationFn: () => deleteConversation(conversation.id),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: [...CONVERSATIONS_KEY, conversation.id] });
      await queryClient.invalidateQueries({ queryKey: CONVERSATIONS_KEY, exact: true });
      if (current) navigate("/", { replace: true });
    },
    onError: () => setError("삭제하지 못했습니다"),
  });

  const submitRename = (e: FormEvent) => {
    e.preventDefault();
    const next = title.trim();
    if (!next) return;
    if (next === conversation.title) {
      setMode("view");
      return;
    }
    rename.mutate(next);
  };

  const name = displayTitle(conversation);

  if (mode === "rename") {
    return (
      <li className="px-2 py-1">
        <form onSubmit={submitRename} className="flex flex-col gap-1">
          <input
            autoFocus
            aria-label="새 대화 이름"
            value={title}
            maxLength={200}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setTitle(conversation.title);
                setMode("view");
              }
            }}
            className="rounded border border-slate-300 px-2 py-1 text-sm outline-none focus:border-slate-500"
          />
          <div className="flex gap-1 text-xs">
            <button
              type="submit"
              disabled={!title.trim() || rename.isPending}
              className="rounded bg-slate-900 px-2 py-0.5 text-white disabled:opacity-40"
            >
              저장
            </button>
            <button
              type="button"
              onClick={() => {
                setTitle(conversation.title);
                setMode("view");
              }}
              className="rounded px-2 py-0.5 hover:bg-slate-100"
            >
              취소
            </button>
          </div>
          {error && <p className="text-xs text-red-600">{error}</p>}
        </form>
      </li>
    );
  }

  return (
    <li className="group">
      <div
        className={cn(
          "flex items-center gap-1 rounded-md px-2 py-1.5",
          current ? "bg-slate-200" : "hover:bg-slate-100",
        )}
      >
        <NavLink
          to={`/c/${conversation.id}`}
          onClick={onNavigate}
          aria-current={current ? "page" : undefined}
          className="min-w-0 flex-1"
        >
          <span className="block truncate text-sm text-slate-800" title={name}>
            {name}
          </span>
          <span className="block text-[11px] text-slate-500">{formatRelative(conversation.updated_at)}</span>
        </NavLink>
        <button
          type="button"
          aria-label={`${name} 이름 변경`}
          title="이름 변경"
          onClick={() => {
            setTitle(conversation.title);
            setMode("rename");
          }}
          className="rounded px-1 text-xs text-slate-500 hover:bg-slate-200 hover:text-slate-800"
        >
          이름
        </button>
        <button
          type="button"
          aria-label={`${name} 삭제`}
          title="삭제"
          onClick={() => setMode("confirmDelete")}
          className="rounded px-1 text-xs text-slate-500 hover:bg-red-100 hover:text-red-700"
        >
          삭제
        </button>
      </div>
      {mode === "confirmDelete" && (
        <div role="alertdialog" aria-label="대화 삭제 확인" className="mx-2 mb-1 rounded-md bg-red-50 p-2 text-xs">
          <p className="text-red-800">이 대화와 메시지, 피드백을 모두 삭제할까요?</p>
          <div className="mt-1.5 flex gap-1">
            <button
              type="button"
              disabled={remove.isPending}
              onClick={() => remove.mutate()}
              className="rounded bg-red-600 px-2 py-0.5 text-white disabled:opacity-40"
            >
              삭제
            </button>
            <button
              type="button"
              onClick={() => setMode("view")}
              className="rounded px-2 py-0.5 hover:bg-red-100"
            >
              취소
            </button>
          </div>
          {error && <p className="mt-1 text-red-600">{error}</p>}
        </div>
      )}
    </li>
  );
}

export function ConversationList({
  currentId,
  onNavigate,
}: {
  currentId: number | null;
  onNavigate?: () => void;
}) {
  const navigate = useNavigate();
  const conversations = useQuery({ queryKey: CONVERSATIONS_KEY, queryFn: listConversations });
  return (
    <div className="flex h-full flex-col">
      <div className="p-2">
        <button
          type="button"
          onClick={() => {
            onNavigate?.();
            navigate("/");
          }}
          className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-100"
        >
          + 새 대화
        </button>
      </div>
      <section aria-label="대화 목록" className="min-h-0 flex-1 overflow-y-auto px-1 pb-2">
        {conversations.isError && <p className="px-2 text-xs text-red-600">대화 목록을 불러오지 못했습니다</p>}
        {conversations.data?.length === 0 && <p className="px-2 text-xs text-slate-500">아직 대화가 없습니다</p>}
        <ul className="space-y-0.5">
          {conversations.data?.map((c) => (
            <Item key={c.id} conversation={c} current={c.id === currentId} onNavigate={onNavigate} />
          ))}
        </ul>
      </section>
    </div>
  );
}
