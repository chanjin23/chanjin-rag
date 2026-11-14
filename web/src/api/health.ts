import { apiFetch } from "./client";

/** `/health` 이상 코드(TRD 8.3, 12.4). 전역 배너가 쓴다. */
export type HealthIssueCode =
  | "db_unavailable"
  | "worker_stale"
  | "worker_missing"
  | "disk_low"
  | "provider_auth";

/**
 * `since`: 같은 이상을 구분하는 시각(연속 실패의 첫 시각, 마지막 하트비트, 처음 발견한 시각).
 * `level`: 이상이 심해지면 바뀌는 값(`disk_low`: 남은 GB). 배너는 code·since·level로 닫음을 기억한다.
 */
export type HealthIssue = {
  code: HealthIssueCode;
  message: string;
  since: string | null;
  level: number | null;
};

export type ProviderCheck = {
  kind: string;
  model: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error_code: string | null;
  last_status_code: number | null;
  failing: boolean;
  failing_since: string | null;
};

export type Health = {
  status: "ok";
  degraded: boolean;
  now: string;
  issues: HealthIssue[];
  checks: {
    db: { ok: boolean; error: string | null };
    worker: {
      status: "ok" | "stale" | "missing" | "unknown";
      name: string | null;
      heartbeat_at: string | null;
      age_seconds: number | null;
      stale_after_seconds: number;
    };
    disk: {
      status: "ok" | "low" | "unknown";
      free_bytes: number | null;
      total_bytes: number | null;
      min_free_bytes: number;
    };
    providers: ProviderCheck[];
  };
};

export const HEALTH_KEY = ["health", "details"] as const;

/** 세부 상태 점검(세션 필요). 공개 `/health`는 상태와 이상 코드만 준다(TRD 8.3). */
export async function getHealth(): Promise<Health> {
  return (await (await apiFetch("/health/details")).json()) as Health;
}
