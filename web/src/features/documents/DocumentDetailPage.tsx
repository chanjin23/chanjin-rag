import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router";
import type { Source } from "@/api/chat";
import { ApiError } from "@/api/client";
import { type DocumentDetail, documentKey, getDocument } from "@/api/documents";
import type { SummarizeRequest } from "@/features/chat/ChatPage";
import { formatDate, formatLocation, formatRelative } from "@/features/chat/format";
import { cn } from "@/lib/cn";
import type { DetailState } from "./links";
import { FAILURE_INFO, formatSize, parseLoc, STATUS_LABEL, STATUS_STYLE, sectionInLocation } from "./status";

/**
 * 문서 상세 (F-10, TRD 9.1): 위치별 추출 텍스트, `?loc=`(인용 위치 JSON)로 스크롤·강조, "이 문서 요약".
 * 출처 패널에서 오면 저장된 인용(`location.state.citation`)을 받는다. 문서가 삭제됐거나(404, 인용 가림)
 * 인용 이후 바뀌었으면(수정 시각이 다름) "원문이 바뀌었거나 삭제되었습니다"와 저장된 인용 구절을 보여준다.
 */

const CHANGED_TEXT = "원문이 바뀌었거나 삭제되었습니다";

function StoredCitation({ citation }: { citation: Source }) {
  const location = formatLocation(citation.location);
  return (
    <div className="mt-2 space-y-1 text-sm">
      <p className="text-slate-600">
        인용 당시: {location && `${location} · `}
        {formatDate(citation.mtime)} 수정본
      </p>
      {citation.snippet && (
        <blockquote
          aria-label="저장된 인용 구절"
          className="whitespace-pre-wrap border-l-2 border-amber-400 pl-2 text-slate-800"
        >
          {citation.snippet}
        </blockquote>
      )}
    </div>
  );
}

function ChangedNotice({ citation, deleted }: { citation?: Source; deleted: boolean }) {
  return (
    <div role="note" className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-amber-900">
      <p className="font-medium">{CHANGED_TEXT}</p>
      {deleted && <p className="text-sm">이 문서는 색인에서 지워졌습니다.</p>}
      {citation && <StoredCitation citation={citation} />}
    </div>
  );
}

function citationChanged(doc: DocumentDetail, citation: Source | undefined): boolean {
  if (!citation) return false;
  if (citation.deleted) return true;
  return new Date(citation.mtime).getTime() !== new Date(doc.mtime).getTime();
}

export function DocumentDetailPage() {
  const { documentId = "" } = useParams();
  const [params] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const citation = (location.state as DetailState)?.citation;
  const loc = parseLoc(params.get("loc"));
  const id = /^\d+$/.test(documentId) ? Number(documentId) : null;

  const detail = useQuery({
    queryKey: documentKey(id ?? 0),
    queryFn: () => getDocument(id!),
    enabled: id !== null,
    retry: (count, err) => !(err instanceof ApiError && err.status === 404) && count < 2,
  });
  const notFound = id === null || (detail.error instanceof ApiError && detail.error.status === 404);
  const doc = detail.data;

  const highlighted = useMemo(() => {
    if (!doc || !loc) return new Set<number>();
    return new Set(doc.sections.filter((s) => sectionInLocation(s.location, loc)).map((s) => s.seq));
  }, [doc, loc]);
  const firstRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (highlighted.size) firstRef.current?.scrollIntoView({ block: "center" });
  }, [highlighted]);

  const summarize = () => {
    if (!doc) return;
    const summarizeRequest: SummarizeRequest = { documentId: doc.id, filename: doc.filename };
    navigate("/", { state: { summarize: summarizeRequest } });
  };

  if (notFound) {
    return (
      <div className="mx-auto max-w-3xl space-y-3 p-4 sm:p-6">
        <h1 className="text-xl font-semibold">{citation ? citation.path.split(/[\\/]/).pop() : "문서 상세"}</h1>
        {citation && <p className="break-all font-mono text-xs text-slate-500">{citation.path}</p>}
        <ChangedNotice citation={citation} deleted />
        <Link to="/documents" className="inline-block text-sm text-sky-700 hover:underline">
          문서 목록으로
        </Link>
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="mx-auto max-w-3xl p-4 sm:p-6">
        <h1 className="text-xl font-semibold">문서 상세</h1>
        <p className="mt-2 text-sm text-slate-500">
          {detail.isError ? "문서를 불러오지 못했습니다." : "불러오는 중…"}
        </p>
      </div>
    );
  }

  const failure = doc.failure_code ? FAILURE_INFO[doc.failure_code] : null;
  let firstAssigned = false;

  return (
    <div className="mx-auto flex h-full max-w-4xl flex-col overflow-y-auto p-4 sm:p-6" data-testid="document-detail">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="break-all text-xl font-semibold">{doc.filename}</h1>
          <p className="break-all font-mono text-xs text-slate-500">{doc.path}</p>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-600">
            <span className={cn("rounded px-1.5 py-0.5 font-medium", STATUS_STYLE[doc.status])}>
              {STATUS_LABEL[doc.status]}
            </span>
            <span>
              {formatSize(doc.size)} · {formatRelative(doc.mtime)} 수정 · 청크 {doc.chunks}개
            </span>
            {doc.locally_deleted && <span className="text-amber-700">로컬에서 삭제됨</span>}
            {doc.needs_upload && <span className="text-sky-700">다음 동기화 대기</span>}
          </p>
        </div>
        <div className="flex shrink-0 gap-2 text-sm">
          <button
            type="button"
            onClick={summarize}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-white hover:bg-slate-700"
          >
            이 문서 요약
          </button>
          <Link to="/documents" className="rounded-md px-3 py-1.5 text-slate-600 hover:bg-slate-100">
            문서 목록
          </Link>
        </div>
      </div>

      {citationChanged(doc, citation) && (
        <div className="mt-3">
          <ChangedNotice citation={citation} deleted={false} />
        </div>
      )}

      {doc.status === "failed" && failure && (
        <div className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-900">
          <p>실패 사유: {failure.reason}</p>
          <p>해결 안내: {failure.hint}</p>
        </div>
      )}

      <h2 className="mt-5 text-sm font-semibold text-slate-700">추출 텍스트</h2>
      {doc.sections.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">
          저장된 추출 텍스트가 없습니다. 파일명으로만 검색됩니다.
        </p>
      ) : (
        <ol aria-label="추출 텍스트" className="mt-2 space-y-2">
          {doc.sections.map((s) => {
            const on = highlighted.has(s.seq);
            const isFirst = on && !firstAssigned;
            if (isFirst) firstAssigned = true;
            const label = formatLocation(s.location);
            return (
              <li
                key={s.seq}
                ref={isFirst ? (el) => void (firstRef.current = el) : undefined}
                data-highlight={on ? "true" : undefined}
                aria-current={on ? "location" : undefined}
                className={cn(
                  "rounded-lg border px-3 py-2",
                  on ? "border-amber-400 bg-amber-50 ring-2 ring-amber-200" : "border-slate-200 bg-white",
                )}
              >
                <p className="text-xs text-slate-500">
                  {label || `#${s.seq + 1}`}
                  {s.heading_path.length > 0 && ` · ${s.heading_path.join(" > ")}`}
                  {on && <span className="ml-1 font-medium text-amber-700">인용 위치</span>}
                </p>
                <p className="mt-1 whitespace-pre-wrap text-sm text-slate-800">{s.text}</p>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
