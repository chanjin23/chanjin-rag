import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import type { Source } from "@/api/chat";
import { cn } from "@/lib/cn";
import { fileName, formatDate, formatLocation, isWindows, revealUrl } from "./format";

/**
 * 출처 패널 (F-10, TRD 9.1, 9.2 "로컬 경로 표시").
 * 항목: 경로(복사 버튼), 위치, 수정일, 인용 구절, 로컬 삭제 표시, 문서 상세 링크,
 * Windows에서만 "폴더에서 보기"(`chanjin-sync://reveal?k=`). 로컬 경로는 링크로 만들지 않는다.
 * 인용 칩을 누르면 `focus`(인용 번호)가 바뀌고 해당 항목을 펼쳐 보여준다.
 */

export type PanelFocus = { n: number; nonce: number };

type Props = {
  sources: Source[];
  notFound: boolean;
  focus: PanelFocus | null;
  /** 요약 후보 문서를 골라 요약을 요청한다(F-09) */
  onSummarize?: (source: Source) => void;
};

const itemKey = (s: Source, i: number) => (s.n !== null ? `n${s.n}` : `c${i}`);

function CopyPathButton({ path }: { path: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(path);
          setCopied(true);
          setTimeout(() => setCopied(false), 2000);
        } catch {
          setCopied(false);
        }
      }}
      className="shrink-0 rounded border border-slate-300 px-1.5 py-0.5 text-xs hover:bg-slate-100"
    >
      {copied ? "복사됨" : "경로 복사"}
    </button>
  );
}

function RevealLink({ path }: { path: string }) {
  const [href, setHref] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    void revealUrl(path).then((url) => alive && setHref(url));
    return () => {
      alive = false;
    };
  }, [path]);
  if (!href) return null;
  return (
    <a href={href} className="rounded border border-slate-300 px-1.5 py-0.5 text-xs hover:bg-slate-100">
      폴더에서 보기
    </a>
  );
}

function SourceItem({
  source,
  open,
  onToggle,
  onSummarize,
  itemRef,
}: {
  source: Source;
  open: boolean;
  onToggle: () => void;
  onSummarize?: (source: Source) => void;
  itemRef?: (el: HTMLLIElement | null) => void;
}) {
  const location = formatLocation(source.location);
  const bodyId = `source-${source.document_id}-${source.chunk_id ?? "doc"}-${source.n ?? "c"}`;
  const detailHref =
    `/documents/${source.document_id}` + (source.chunk_id !== null ? `?chunk=${source.chunk_id}` : "");
  return (
    <li ref={itemRef} className={cn("rounded-lg border bg-white", open ? "border-sky-300" : "border-slate-200")}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={onToggle}
        className="flex w-full items-start gap-1.5 px-3 py-2 text-left text-xs"
      >
        {source.n !== null && <span className="font-semibold text-sky-800">[{source.n}]</span>}
        <span className="min-w-0 flex-1">
          <span className="block truncate font-medium text-slate-800" title={source.path}>
            {fileName(source.path)}
          </span>
          <span className="block text-slate-500">
            {location && `${location} · `}
            {formatDate(source.mtime)} 수정
            {source.locally_deleted && <span className="text-amber-700"> · 로컬에서 삭제됨</span>}
          </span>
        </span>
      </button>
      {open && (
        <div id={bodyId} className="space-y-2 border-t border-slate-100 px-3 py-2 text-xs" data-testid="source-detail">
          <div className="flex items-start gap-2">
            <span className="min-w-0 flex-1 break-all font-mono text-[11px] text-slate-700" data-testid="source-path">
              {source.path}
            </span>
            <CopyPathButton path={source.path} />
          </div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-slate-600">
            {location && (
              <>
                <dt>위치</dt>
                <dd>{location}</dd>
              </>
            )}
            <dt>수정일</dt>
            <dd>{formatDate(source.mtime)}</dd>
          </dl>
          {source.locally_deleted && (
            <p className="rounded bg-amber-50 px-2 py-1 text-amber-800">
              내 PC에서 삭제된 파일입니다. 색인에는 30일 동안 남습니다.
            </p>
          )}
          {source.snippet && (
            <blockquote className="whitespace-pre-wrap border-l-2 border-sky-300 pl-2 text-slate-700" aria-label="인용 구절">
              {source.snippet}
            </blockquote>
          )}
          <div className="flex flex-wrap gap-2">
            <Link
              to={detailHref}
              className="rounded border border-slate-300 px-1.5 py-0.5 hover:bg-slate-100"
            >
              문서 상세
            </Link>
            {isWindows() && <RevealLink path={source.path} />}
            {onSummarize && (
              <button
                type="button"
                onClick={() => onSummarize(source)}
                className="rounded border border-slate-300 px-1.5 py-0.5 hover:bg-slate-100"
              >
                이 문서 요약
              </button>
            )}
          </div>
        </div>
      )}
    </li>
  );
}

export function SourcePanel({ sources, notFound, focus, onSummarize }: Props) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const refs = useRef(new Map<string, HTMLLIElement>());

  // 인용 칩을 누르면 해당 항목을 펼치고 보이게 한다
  useEffect(() => {
    if (!focus) return;
    const key = `n${focus.n}`;
    setOpen((prev) => new Set(prev).add(key));
    const el = refs.current.get(key);
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    el?.querySelector("button")?.focus({ preventScroll: true });
  }, [focus]);

  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const cited = sources.filter((s) => s.n !== null);
  const others = sources.filter((s) => s.n === null);

  const renderList = (label: string, list: Source[], summarize: boolean) => (
    <section className="space-y-1.5">
      <h3 className="text-xs font-medium text-slate-500">{label}</h3>
      <ul aria-label={label} className="space-y-1.5">
        {list.map((s) => {
          const key = itemKey(s, sources.indexOf(s));
          return (
            <SourceItem
              key={key}
              source={s}
              open={open.has(key)}
              onToggle={() => toggle(key)}
              onSummarize={summarize ? onSummarize : undefined}
              itemRef={(el) => {
                if (el) refs.current.set(key, el);
                else refs.current.delete(key);
              }}
            />
          );
        })}
      </ul>
    </section>
  );

  if (sources.length === 0) {
    return <p className="text-xs text-slate-500">답변의 출처가 여기에 표시됩니다.</p>;
  }
  return (
    <div className="space-y-4">
      {cited.length > 0 && renderList("출처", cited, false)}
      {others.length > 0 &&
        renderList(notFound ? "참고 후보" : "후보 문서", others, !notFound && !!onSummarize)}
    </div>
  );
}
