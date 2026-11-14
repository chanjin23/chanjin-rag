import { apiFetch } from "@/api/client";
import type { FeedbackReason } from "@/api/conversations";

/**
 * 평가 API (TRD 8.3 `/eval/runs*`, F-13)와 싫어요 피드백 검토 API(`/feedback`, F-12, PRD 8장).
 * 백엔드 app/api/eval.py, app/api/feedback.py와 맞춘다.
 */
export type MetricKey =
  | "recall_at_5"
  | "mrr"
  | "faithfulness"
  | "answer_accuracy"
  | "not_found_accuracy"
  | "p95_ms";

export type MetricDef = {
  key: MetricKey;
  label: string;
  target: number;
  higher_is_better: boolean;
};

export type EvalMetrics = Partial<Record<MetricKey, number | null>> & {
  items?: number;
  evaluated?: number;
  excluded?: number;
  errors?: number;
};

export type EvalRunSummary = {
  id: number;
  dataset_version: string;
  git_sha: string | null;
  judge_model: string | null;
  llm_model: string | null;
  started_at: string;
  finished_at: string | null;
  metrics: EvalMetrics;
  previous_id: number | null;
  previous_metrics: EvalMetrics | null;
  deltas: Record<MetricKey, number | null>;
};

export type EvalRunList = { metrics: MetricDef[]; items: EvalRunSummary[] };

export type EvalItemResult = {
  item_id: string;
  answer: string | null;
  retrieved: { rank: number; path: string; location: Record<string, unknown> }[];
  scores: {
    excluded?: boolean;
    missing_paths?: string[];
    type?: string;
    expects_not_found?: boolean;
    not_found?: boolean;
    recall_at_5?: number | null;
    reciprocal_rank?: number | null;
    verdict?: "correct" | "partial" | "wrong" | null;
    faithfulness?: number | null;
    error?: string | null;
  };
};

export type EvalRunDetail = EvalRunSummary & {
  config: Record<string, unknown>;
  results: EvalItemResult[];
};

export const EVAL_RUNS_KEY = ["eval", "runs"] as const;

export async function listEvalRuns(): Promise<EvalRunList> {
  return (await apiFetch("/eval/runs")).json() as Promise<EvalRunList>;
}

export async function getEvalRun(id: number): Promise<EvalRunDetail> {
  return (await apiFetch(`/eval/runs/${id}`)).json() as Promise<EvalRunDetail>;
}

export type FailureType = "parse" | "chunk" | "retrieval" | "rerank" | "hallucination";

export type FeedbackItem = {
  id: number;
  message_id: number;
  conversation_id: number;
  rating: 1 | -1;
  reason: FeedbackReason | null;
  note: string | null;
  failure_type: FailureType | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
  question: string | null;
  answer: string;
};

export type FeedbackList = { items: FeedbackItem[]; total: number; unreviewed: number };

export const DISLIKES_KEY = ["feedback", "dislikes"] as const;

export async function listDislikes(): Promise<FeedbackList> {
  return (await apiFetch("/feedback?rating=-1")).json() as Promise<FeedbackList>;
}

export type FeedbackReviewed = {
  id: number;
  message_id: number;
  failure_type: FailureType | null;
  reviewed_at: string;
};

export async function reviewFeedback(
  id: number,
  failureType: FailureType | null,
): Promise<FeedbackReviewed> {
  const res = await apiFetch(`/feedback/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ failure_type: failureType }),
  });
  return res.json() as Promise<FeedbackReviewed>;
}
