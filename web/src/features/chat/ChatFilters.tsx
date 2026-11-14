import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { ChatFilters } from "@/api/chat";
import { DOCUMENTS_KEY, suggestFolders } from "@/api/documents";

/**
 * 질문 필터 (F-07, TRD 9.1): 디렉터리(경로 앞부분), 형식, 수정일(한국 시간 날짜), 로컬 삭제 포함.
 * 화면 상태로만 두고 질문마다 함께 보낸다. 디렉터리는 `/documents/suggest`로 폴더 경로를 자동완성한다.
 */

export const EXT_OPTIONS = ["md", "txt", "pdf", "docx", "pptx", "xlsx", "hwp", "hwpx"] as const;

export type FilterState = {
  directory: string;
  exts: string[];
  modifiedFrom: string;
  modifiedTo: string;
  includeDeleted: boolean;
};

export const EMPTY_FILTERS: FilterState = {
  directory: "",
  exts: [],
  modifiedFrom: "",
  modifiedTo: "",
  includeDeleted: true,
};

/** 화면 상태 → API 필터. 아무것도 고르지 않았으면 undefined(필터 없음). */
export function toApiFilters(f: FilterState): ChatFilters | undefined {
  const out: ChatFilters = {};
  if (f.directory.trim()) out.directory = f.directory.trim();
  if (f.exts.length) out.exts = f.exts;
  if (f.modifiedFrom) out.modified_from = f.modifiedFrom;
  if (f.modifiedTo) out.modified_to = f.modifiedTo;
  if (!f.includeDeleted) out.include_deleted = false;
  return Object.keys(out).length ? out : undefined;
}

export function activeFilterCount(f: FilterState): number {
  return Object.keys(toApiFilters(f) ?? {}).length;
}

type Props = { value: FilterState; onChange: (next: FilterState) => void };

/** 입력이 멈추고 잠시 뒤의 값(자동완성 요청을 줄인다) */
function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

export function ChatFiltersForm({ value, onChange }: Props) {
  const set = (patch: Partial<FilterState>) => onChange({ ...value, ...patch });
  const prefix = useDebounced(value.directory, 200);
  const suggestions = useQuery({
    queryKey: [...DOCUMENTS_KEY, "suggest", prefix],
    queryFn: () => suggestFolders(prefix),
    staleTime: 30_000,
  });
  return (
    <fieldset className="mb-2 grid gap-2 rounded-lg border border-slate-200 bg-white p-2 text-xs sm:grid-cols-2">
      <legend className="sr-only">검색 필터</legend>
      <label className="flex flex-col gap-0.5 sm:col-span-2">
        <span className="text-slate-500">디렉터리</span>
        <input
          value={value.directory}
          onChange={(e) => set({ directory: e.target.value })}
          placeholder="C:\Chanjin\dev\문서"
          list="directory-suggestions"
          autoComplete="off"
          className="rounded border border-slate-300 px-2 py-1 outline-none focus:border-slate-500"
        />
        <datalist id="directory-suggestions" data-testid="directory-suggestions">
          {(suggestions.data ?? []).map((folder) => (
            <option key={folder} value={folder} />
          ))}
        </datalist>
      </label>
      <div className="flex flex-col gap-0.5 sm:col-span-2">
        <span className="text-slate-500">형식</span>
        <div className="flex flex-wrap gap-2">
          {EXT_OPTIONS.map((ext) => (
            <label key={ext} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={value.exts.includes(ext)}
                onChange={(e) =>
                  set({
                    exts: e.target.checked
                      ? [...value.exts, ext]
                      : value.exts.filter((x) => x !== ext),
                  })
                }
              />
              {ext.toUpperCase()}
            </label>
          ))}
        </div>
      </div>
      <label className="flex flex-col gap-0.5">
        <span className="text-slate-500">수정일 시작</span>
        <input
          type="date"
          value={value.modifiedFrom}
          onChange={(e) => set({ modifiedFrom: e.target.value })}
          className="rounded border border-slate-300 px-2 py-1"
        />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-slate-500">수정일 끝</span>
        <input
          type="date"
          value={value.modifiedTo}
          onChange={(e) => set({ modifiedTo: e.target.value })}
          className="rounded border border-slate-300 px-2 py-1"
        />
      </label>
      <label className="flex items-center gap-1 sm:col-span-2">
        <input
          type="checkbox"
          checked={value.includeDeleted}
          onChange={(e) => set({ includeDeleted: e.target.checked })}
        />
        로컬에서 삭제된 문서 포함
      </label>
      <div className="sm:col-span-2">
        <button
          type="button"
          onClick={() => onChange(EMPTY_FILTERS)}
          className="rounded px-2 py-0.5 text-slate-500 hover:bg-slate-100"
        >
          필터 초기화
        </button>
      </div>
    </fieldset>
  );
}
