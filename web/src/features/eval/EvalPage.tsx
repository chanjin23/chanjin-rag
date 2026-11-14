import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { ApiError } from "@/api/client";
import {
  DISLIKES_KEY,
  EVAL_RUNS_KEY,
  type EvalItemResult,
  type EvalMetrics,
  type EvalRunSummary,
  type FailureType,
  type FeedbackItem,
  getEvalRun,
  listDislikes,
  listEvalRuns,
  type MetricDef,
  type MetricKey,
  reviewFeedback,
} from "@/api/eval";
import { formatRelative } from "@/features/chat/format";
import { cn } from "@/lib/cn";

/**
 * 평가 화면 (TRD 9.1 `/eval`, F-12, F-13, PRD 8장).
 * - 평가 실행 목록(`rag-eval run`이 저장한 실행), 고른 실행의 지표 표와 바로 이전 실행 대비 증감
 * - 문항별 결과(제외된 문항은 이유와 함께)
 * - 싫어요 피드백 검토: 실패 유형(파싱, 청킹, 검색 누락, 재정렬, 생성 환각)을 붙인다
 */

export const FAILURE_TYPES: { value: FailureType; label: string }[] = [
  { value: "parse", label: "파싱" },
  { value: "chunk", label: "청킹" },
  { value: "retrieval", label: "검색 누락" },
  { value: "rerank", label: "재정렬" },
  { value: "hallucination", label: "생성 환각" },
];

const REASON_LABEL: Record<string, string> = {
  wrong: "틀림",
  not_found_wrong: "실제로는 문서에 있음",
  other: "기타",
};

const VERDICT_LABEL: Record<string, string> = { correct: "정답", partial: "부분", wrong: "오답" };

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "요청을 처리하지 못했습니다";
}

export function formatMetric(key: MetricKey, value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return key === "p95_ms" ? `${Math.round(value).toLocaleString("ko-KR")}ms` : value.toFixed(3);
}

export function formatDelta(key: MetricKey, value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  const sign = value > 0 ? "+" : value < 0 ? "-" : "±";
  const abs = Math.abs(value);
  return key === "p95_ms" ? `${sign}${Math.round(abs).toLocaleString("ko-KR")}ms` : `${sign}${abs.toFixed(3)}`;
}

function deltaTone(def: MetricDef, value: number | null | undefined): string {
  if (!value) return "text-slate-500";
  const better = def.higher_is_better ? value > 0 : value < 0;
  return better ? "text-emerald-700" : "text-rose-700";
}

function meetsTarget(def: MetricDef, value: number | null | undefined): boolean | null {
  if (value === null || value === undefined) return null;
  return def.higher_is_better ? value >= def.target : value <= def.target;
}

// ---------------------------------------------------------------------------
// 실행 목록
// ---------------------------------------------------------------------------

function RunList({
  runs,
  selectedId,
  onSelect,
}: {
  runs: EvalRunSummary[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  return (
    <section aria-label="평가 실행" className="rounded-lg border border-slate-200 bg-white p-3">
      <h2 className="text-sm font-medium">평가 실행</h2>
      {runs.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">
          아직 평가 실행이 없습니다. 서버에서 <code>rag-eval run</code>으로 평가셋을 실행하세요.
        </p>
      ) : (
        <ul aria-label="평가 실행 목록" className="mt-2 divide-y divide-slate-100">
          {runs.map((run) => (
            <li key={run.id} aria-label={`실행 #${run.id}`}>
              <button
                type="button"
                aria-pressed={run.id === selectedId}
                onClick={() => onSelect(run.id)}
                className={cn(
                  "flex w-full flex-wrap items-baseline gap-x-3 px-2 py-1.5 text-left text-sm",
                  run.id === selectedId ? "bg-slate-100" : "hover:bg-slate-50",
                )}
              >
                <span className="font-medium">실행 #{run.id}</span>
                <span className="text-slate-600">{run.dataset_version}</span>
                <span className="text-xs text-slate-500">
                  {run.finished_at ? formatRelative(run.finished_at) : "진행 중"}
                  {" · "}문항 {run.metrics.items ?? 0}개
                  {run.metrics.excluded ? ` (제외 ${run.metrics.excluded})` : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// 지표 표
// ---------------------------------------------------------------------------

function MetricsTable({ defs, run }: { defs: MetricDef[]; run: EvalRunSummary }) {
  const previous: EvalMetrics | null = run.previous_metrics;
  return (
    <section aria-label="지표" className="rounded-lg border border-slate-200 bg-white p-3">
      <h2 className="text-sm font-medium">
        실행 #{run.id} 지표
        <span className="ml-2 text-xs font-normal text-slate-500">
          {run.previous_id !== null ? `이전 실행 #${run.previous_id} 대비` : "이전 실행 없음"}
        </span>
      </h2>
      <p className="mt-1 text-xs text-slate-500">
        평가셋 {run.dataset_version} · 답변 모델 {run.llm_model ?? "-"} · 판정 모델 {run.judge_model ?? "-"}
        {run.git_sha ? ` · ${run.git_sha}` : ""}
        {` · 평가 ${run.metrics.evaluated ?? 0} · 제외 ${run.metrics.excluded ?? 0} · 오류 ${run.metrics.errors ?? 0}`}
      </p>
      <table aria-label="지표 표" className="mt-2 w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-slate-500">
            <th className="py-1 font-normal">지표</th>
            <th className="py-1 text-right font-normal">이번</th>
            <th className="py-1 text-right font-normal">이전</th>
            <th className="py-1 text-right font-normal">증감</th>
            <th className="py-1 text-right font-normal">목표</th>
          </tr>
        </thead>
        <tbody>
          {defs.map((def) => {
            const value = run.metrics[def.key];
            const met = meetsTarget(def, value);
            return (
              <tr key={def.key} className="border-t border-slate-100">
                <th scope="row" className="py-1 text-left font-medium">
                  {def.label}
                </th>
                <td
                  className={cn(
                    "py-1 text-right tabular-nums",
                    met === false && "text-rose-700",
                    met === true && "text-emerald-700",
                  )}
                >
                  {formatMetric(def.key, value)}
                </td>
                <td className="py-1 text-right tabular-nums text-slate-500">
                  {previous ? formatMetric(def.key, previous[def.key]) : "-"}
                </td>
                <td
                  aria-label={`${def.label} 증감`}
                  className={cn("py-1 text-right tabular-nums", deltaTone(def, run.deltas[def.key]))}
                >
                  {formatDelta(def.key, run.deltas[def.key])}
                </td>
                <td className="py-1 text-right tabular-nums text-slate-500">
                  {def.higher_is_better ? "≥" : "≤"} {formatMetric(def.key, def.target)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function resultSummary(r: EvalItemResult): string {
  const s = r.scores;
  if (s.excluded) return `제외: 근거 경로 ${s.missing_paths?.length ?? 0}개가 문서 목록에 없음`;
  if (s.error) return `오류: ${s.error}`;
  if (s.expects_not_found) return s.not_found ? "찾지 못함 (맞음)" : "답을 함 (찾지 못함 기대)";
  const parts = [
    `Recall@5 ${s.recall_at_5 ?? "-"}`,
    `RR ${s.reciprocal_rank !== null && s.reciprocal_rank !== undefined ? s.reciprocal_rank.toFixed(2) : "-"}`,
    `판정 ${s.verdict ? VERDICT_LABEL[s.verdict] : "-"}`,
  ];
  if (s.faithfulness !== null && s.faithfulness !== undefined) parts.push(`충실성 ${s.faithfulness.toFixed(2)}`);
  return parts.join(" · ");
}

function ItemResults({ runId }: { runId: number }) {
  const detail = useQuery({ queryKey: [...EVAL_RUNS_KEY, runId], queryFn: () => getEvalRun(runId) });
  if (detail.isPending) return <p className="text-sm text-slate-500">문항 결과를 불러오는 중…</p>;
  if (detail.isError) return <p className="text-sm text-rose-700">{errorMessage(detail.error)}</p>;
  return (
    <section aria-label="문항별 결과" className="rounded-lg border border-slate-200 bg-white p-3">
      <h2 className="text-sm font-medium">문항별 결과</h2>
      <ul className="mt-2 space-y-1 text-sm">
        {detail.data.results.map((r) => (
          <li key={r.item_id} aria-label={`문항 ${r.item_id}`} className="flex flex-wrap gap-x-2">
            <span className="font-mono text-xs text-slate-600">{r.item_id}</span>
            {r.scores.type && <span className="text-xs text-slate-400">{r.scores.type}</span>}
            <span className={cn("text-xs", r.scores.excluded ? "text-amber-800" : "text-slate-700")}>
              {resultSummary(r)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// 싫어요 피드백 검토
// ---------------------------------------------------------------------------

function DislikeItem({ item }: { item: FeedbackItem }) {
  const qc = useQueryClient();
  const [choice, setChoice] = useState<FailureType | "">(item.failure_type ?? "");
  const [notice, setNotice] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => reviewFeedback(item.id, choice === "" ? null : choice),
    onSuccess: () => {
      setNotice("실패 유형을 저장했습니다");
      void qc.invalidateQueries({ queryKey: DISLIKES_KEY });
    },
    onError: (err) => setNotice(errorMessage(err)),
  });
  const typeLabel = FAILURE_TYPES.find((t) => t.value === item.failure_type)?.label;
  return (
    <li aria-label={`피드백 #${item.id}`} className="space-y-1 py-2">
      <p className="text-sm font-medium">{item.question ?? "(질문 없음)"}</p>
      <p className="line-clamp-3 whitespace-pre-wrap text-xs text-slate-600">{item.answer || "(빈 답변)"}</p>
      <p className="text-xs text-slate-500">
        {item.reason ? `사유: ${REASON_LABEL[item.reason]}` : "사유 없음"}
        {item.note ? ` · 메모: ${item.note}` : ""}
        {" · "}
        {formatRelative(item.updated_at)}
        {" · "}
        <Link to={`/c/${item.conversation_id}`} className="underline">
          대화 보기
        </Link>
      </p>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span
          role="status"
          className={cn(
            "rounded px-2 py-0.5",
            item.reviewed_at ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-900",
          )}
        >
          {item.reviewed_at
            ? `검토함 · ${typeLabel ?? "유형 없음"} · ${formatRelative(item.reviewed_at)}`
            : "검토 전"}
        </span>
        <label className="flex items-center gap-1">
          실패 유형
          <select
            value={choice}
            onChange={(e) => setChoice(e.target.value as FailureType | "")}
            className="rounded border border-slate-300 px-1 py-0.5"
          >
            <option value="">분류 없음</option>
            {FAILURE_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          disabled={save.isPending}
          onClick={() => {
            setNotice(null);
            save.mutate();
          }}
          className="rounded bg-slate-900 px-2 py-0.5 text-white disabled:opacity-40"
        >
          유형 저장
        </button>
        {notice && <span aria-live="polite">{notice}</span>}
      </div>
    </li>
  );
}

function DislikeReview() {
  const dislikes = useQuery({ queryKey: DISLIKES_KEY, queryFn: listDislikes });
  return (
    <section aria-label="싫어요 피드백 검토" className="rounded-lg border border-slate-200 bg-white p-3">
      <h2 className="text-sm font-medium">
        싫어요 피드백 검토
        {dislikes.data && (
          <span className="ml-2 text-xs font-normal text-slate-500">
            {dislikes.data.total}건 · 검토 전 {dislikes.data.unreviewed}건
          </span>
        )}
      </h2>
      <p className="mt-1 text-xs text-slate-500">
        답변 화면의 싫어요에 실패 유형을 붙입니다. 자주 틀리는 사례는 평가셋에 추가해 회귀를 막으세요.
      </p>
      {dislikes.isPending && <p className="mt-2 text-sm text-slate-500">불러오는 중…</p>}
      {dislikes.isError && <p className="mt-2 text-sm text-rose-700">{errorMessage(dislikes.error)}</p>}
      {dislikes.data &&
        (dislikes.data.items.length === 0 ? (
          <p className="mt-2 text-sm text-slate-500">싫어요 피드백이 없습니다.</p>
        ) : (
          <ul aria-label="싫어요 피드백" className="mt-1 divide-y divide-slate-100">
            {dislikes.data.items.map((item) => (
              <DislikeItem key={`${item.id}-${item.reviewed_at ?? ""}`} item={item} />
            ))}
          </ul>
        ))}
    </section>
  );
}

// ---------------------------------------------------------------------------

export function EvalPage() {
  const runs = useQuery({ queryKey: EVAL_RUNS_KEY, queryFn: listEvalRuns });
  const [picked, setPicked] = useState<number | null>(null);
  const items = runs.data?.items ?? [];
  const selected = items.find((r) => r.id === picked) ?? items[0] ?? null;

  return (
    <div className="mx-auto max-w-4xl space-y-3 p-4">
      <h1 className="text-lg font-semibold">평가</h1>
      {runs.isPending && <p className="text-sm text-slate-500">불러오는 중…</p>}
      {runs.isError && <p className="text-sm text-rose-700">{errorMessage(runs.error)}</p>}
      {runs.data && (
        <>
          <RunList runs={items} selectedId={selected?.id ?? null} onSelect={setPicked} />
          {selected && <MetricsTable defs={runs.data.metrics} run={selected} />}
          {selected && <ItemResults runId={selected.id} />}
        </>
      )}
      <DislikeReview />
    </div>
  );
}
