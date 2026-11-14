import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import {
  DOCUMENTS_KEY,
  type DocumentItem,
  listDocuments,
  purgeDocument,
  type ReprocessResult,
  reprocessDocument,
} from "@/api/documents";
import { INDEX_COVERAGE_KEY } from "@/api/conversations";
import { formatRelative } from "@/features/chat/format";
import { cn } from "@/lib/cn";
import {
  canReprocess,
  FAILURE_INFO,
  formatSize,
  STATUS_LABEL,
  STATUS_STYLE,
  TABS,
  type TabKey,
  tabCount,
} from "./status";

/**
 * 문서 화면 (F-05, F-19, TRD 9.1): 상태별 탭, 실패 사유와 해결 안내(TRD 3.4), 재처리,
 * 영구 삭제(확인 창에 백업·외부 전송 안내, TRD 6.5). 탭·검색어·경로·쪽은 URL 쿼리로 둔다.
 */

const PAGE_SIZE = 50;

/** TRD 6.5 확인 문구 */
export const PURGE_NOTICE =
  "백업 파일에는 최대 7일(PC)까지 남습니다. 이미 NVIDIA API로 보낸 텍스트는 되돌릴 수 없습니다.";

function StatusBadge({ doc }: { doc: DocumentItem }) {
  return (
    <span className={cn("rounded px-1.5 py-0.5 text-xs font-medium", STATUS_STYLE[doc.status])}>
      {STATUS_LABEL[doc.status]}
    </span>
  );
}

function PurgeDialog({
  doc,
  pending,
  error,
  onConfirm,
  onCancel,
}: {
  doc: DocumentItem;
  pending: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => cancelRef.current?.focus(), []);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="purge-title"
        aria-describedby="purge-desc"
        className="w-full max-w-md rounded-lg bg-white p-4 shadow-lg"
        onKeyDown={(e) => e.key === "Escape" && onCancel()}
      >
        <h2 id="purge-title" className="font-semibold">
          문서를 영구 삭제할까요?
        </h2>
        <div id="purge-desc" className="mt-2 space-y-2 text-sm text-slate-700">
          <p className="break-all font-medium">{doc.filename}</p>
          <p>
            추출 텍스트와 검색 색인을 모두 지웁니다. 예전 답변의 인용 구절은 "삭제된 문서"로 가립니다. 내
            PC의 원본 파일은 지우지 않습니다.
          </p>
          <p className="rounded bg-amber-50 px-2 py-1 text-amber-900">{PURGE_NOTICE}</p>
        </div>
        {error && (
          <p role="alert" className="mt-2 text-sm text-red-700">
            {error}
          </p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded-md px-3 py-1.5 text-sm hover:bg-slate-100"
          >
            취소
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={pending}
            className="rounded-md bg-red-600 px-3 py-1.5 text-sm text-white hover:bg-red-700 disabled:opacity-50"
          >
            영구 삭제
          </button>
        </div>
      </div>
    </div>
  );
}

function DocumentRow({
  doc,
  notice,
  reprocessing,
  onReprocess,
  onPurge,
}: {
  doc: DocumentItem;
  notice: ReprocessResult["action"] | undefined;
  reprocessing: boolean;
  onReprocess: () => void;
  onPurge: () => void;
}) {
  const navigate = useNavigate();
  const failure = doc.failure_code ? FAILURE_INFO[doc.failure_code] : null;
  return (
    <li className="rounded-lg border border-slate-200 bg-white px-3 py-2" aria-label={doc.filename}>
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Link to={`/documents/${doc.id}`} className="font-medium text-slate-900 hover:underline">
              {doc.filename}
            </Link>
            <StatusBadge doc={doc} />
            {doc.locally_deleted && (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">로컬에서 삭제됨</span>
            )}
          </div>
          <p className="truncate font-mono text-[11px] text-slate-500" title={doc.path}>
            {doc.path}
          </p>
          <p className="text-xs text-slate-500">
            {formatSize(doc.size)} · {formatRelative(doc.mtime)} 수정 · 청크 {doc.chunks}개
          </p>
        </div>
        <div className="flex shrink-0 gap-1.5 text-xs">
          {canReprocess(doc.status) && !doc.needs_upload && (
            <button
              type="button"
              onClick={onReprocess}
              disabled={reprocessing}
              className="rounded border border-slate-300 px-2 py-1 hover:bg-slate-100 disabled:opacity-50"
            >
              재처리
            </button>
          )}
          <button
            type="button"
            onClick={onPurge}
            className="rounded border border-red-200 px-2 py-1 text-red-700 hover:bg-red-50"
          >
            영구 삭제
          </button>
        </div>
      </div>
      {doc.status === "failed" && (
        <div className="mt-2 rounded bg-red-50 px-2 py-1.5 text-xs text-red-900" data-testid="failure">
          <p>
            <span className="font-medium">실패 사유:</span> {failure?.reason ?? "알 수 없음"}
            {doc.failure_code && <span className="text-red-700"> ({doc.failure_code})</span>}
          </p>
          {failure && (
            <p>
              <span className="font-medium">해결 안내:</span> {failure.hint}
            </p>
          )}
          {doc.failure_detail && <p className="mt-0.5 break-all text-red-700">{doc.failure_detail}</p>}
        </div>
      )}
      {doc.needs_upload && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded bg-sky-50 px-2 py-1.5 text-xs text-sky-900">
          <span className="font-medium">다음 동기화 대기</span>
          <span>다음 동기화 때 파일을 다시 올려 처리합니다.</span>
          <button
            type="button"
            onClick={() => navigate("/sync")}
            className="rounded border border-sky-300 bg-white px-2 py-0.5 hover:bg-sky-100"
          >
            지금 동기화
          </button>
        </div>
      )}
      {notice === "rechunk" && (
        <p className="mt-2 rounded bg-sky-50 px-2 py-1.5 text-xs text-sky-900">
          저장된 추출 텍스트로 다시 청킹합니다. 끝나면 상태가 바뀝니다.
        </p>
      )}
    </li>
  );
}

export function DocumentsPage() {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const tabKey = (params.get("tab") as TabKey | null) ?? "all";
  const tab = TABS.find((t) => t.key === tabKey) ?? TABS[0]!;
  const q = params.get("q") ?? "";
  const path = params.get("path") ?? "";
  const page = Math.max(1, Number(params.get("page") ?? "1") || 1);

  const [qInput, setQInput] = useState(q);
  const [pathInput, setPathInput] = useState(path);
  const [notices, setNotices] = useState<Record<number, ReprocessResult["action"]>>({});
  const [purging, setPurging] = useState<DocumentItem | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const update = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    setParams(next);
  };

  const list = useQuery({
    queryKey: [...DOCUMENTS_KEY, "list", tab.key, q, path, page],
    queryFn: () => listDocuments({ ...tab.query, q, path, page, pageSize: PAGE_SIZE }),
    placeholderData: keepPreviousData,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: DOCUMENTS_KEY });
    void queryClient.invalidateQueries({ queryKey: INDEX_COVERAGE_KEY });
  };

  const reprocess = useMutation({
    mutationFn: (doc: DocumentItem) => reprocessDocument(doc.id),
    onSuccess: (result) => {
      setNotices((prev) => ({ ...prev, [result.id]: result.action }));
      setMessage(null);
      invalidate();
    },
    onError: (err) => setMessage(err instanceof Error ? err.message : "재처리하지 못했습니다"),
  });

  const purge = useMutation({
    mutationFn: (doc: DocumentItem) => purgeDocument(doc.id),
    onSuccess: (_, doc) => {
      setPurging(null);
      setMessage(`「${doc.filename}」을(를) 영구 삭제했습니다.`);
      invalidate();
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    update({ q: qInput.trim() || null, path: pathInput.trim() || null, page: null });
  };

  const data = list.data;
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div className="mx-auto flex h-full max-w-5xl flex-col overflow-y-auto p-4 sm:p-6">
      <h1 className="text-xl font-semibold">문서</h1>
      <p className="mt-1 text-sm text-slate-600">
        파일별 처리 상태와 실패 사유를 보고, 재처리하거나 영구 삭제합니다.
      </p>

      <form onSubmit={submit} className="mt-4 flex flex-wrap gap-2 text-sm" role="search">
        <input
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          placeholder="파일명·경로 검색"
          aria-label="문서 검색어"
          className="min-w-40 flex-1 rounded border border-slate-300 px-2 py-1"
        />
        <input
          value={pathInput}
          onChange={(e) => setPathInput(e.target.value)}
          placeholder="폴더 경로 (예: C:\Chanjin\dev\문서)"
          aria-label="폴더 경로"
          className="min-w-40 flex-1 rounded border border-slate-300 px-2 py-1"
        />
        <button type="submit" className="rounded-md bg-slate-900 px-3 py-1 text-white">
          찾기
        </button>
      </form>

      <div role="tablist" aria-label="문서 상태" className="mt-4 flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map((t) => {
          const n = tabCount(t, data?.counts);
          const selected = t.key === tab.key;
          return (
            <button
              key={t.key}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => update({ tab: t.key === "all" ? null : t.key, page: null })}
              className={cn(
                "-mb-px rounded-t-md border-b-2 px-3 py-1.5 text-sm",
                selected
                  ? "border-slate-900 font-medium text-slate-900"
                  : "border-transparent text-slate-500 hover:text-slate-800",
              )}
            >
              {t.label}
              {n !== null && <span className="ml-1 text-xs text-slate-400">{n}</span>}
            </button>
          );
        })}
      </div>

      {message && (
        <p role="status" className="mt-3 rounded bg-slate-100 px-3 py-2 text-sm text-slate-700">
          {message}
        </p>
      )}

      <div role="tabpanel" aria-label={tab.label} className="mt-3 flex-1">
        {list.isError && <p className="text-sm text-red-700">문서 목록을 불러오지 못했습니다.</p>}
        {data && data.items.length === 0 && (
          <p className="py-8 text-center text-sm text-slate-500">해당하는 문서가 없습니다.</p>
        )}
        {data && data.items.length > 0 && (
          <ul aria-label="문서 목록" className="space-y-2">
            {data.items.map((doc) => (
              <DocumentRow
                key={doc.id}
                doc={doc}
                notice={notices[doc.id]}
                reprocessing={reprocess.isPending && reprocess.variables?.id === doc.id}
                onReprocess={() => reprocess.mutate(doc)}
                onPurge={() => {
                  purge.reset();
                  setPurging(doc);
                }}
              />
            ))}
          </ul>
        )}
        {data && data.total > data.page_size && (
          <div role="group" aria-label="쪽 이동" className="mt-4 flex items-center justify-center gap-3 text-sm">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => update({ page: String(page - 1) })}
              className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40"
            >
              이전
            </button>
            <span>
              {page} / {pages}쪽 · 전체 {data.total}개
            </span>
            <button
              type="button"
              disabled={page >= pages}
              onClick={() => update({ page: String(page + 1) })}
              className="rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-40"
            >
              다음
            </button>
          </div>
        )}
      </div>

      {purging && (
        <PurgeDialog
          doc={purging}
          pending={purge.isPending}
          error={purge.isError ? "삭제하지 못했습니다. 다시 시도하세요." : null}
          onConfirm={() => purge.mutate(purging)}
          onCancel={() => setPurging(null)}
        />
      )}
    </div>
  );
}
