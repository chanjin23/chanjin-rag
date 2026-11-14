import { apiFetch } from "@/api/client";

/**
 * 동기화 화면 API (TRD 8.3, 9.3, F-01~F-03). 백엔드 app/api/settings.py, app/api/sync_web.py와 맞춘다.
 */
export type ExcludeItem = { pattern: string; fixed: boolean };

export type SyncSettings = {
  allowed_root: string;
  roots: string[];
  /** 고정 제외(`fixed: true`, 웹에서 지울 수 없음)가 앞에 온다 */
  excludes: ExcludeItem[];
  max_file_mb: number;
  updated_at: string | null;
};

export type SyncSettingsInput = { roots: string[]; excludes: string[] };

export type SyncPreview = {
  count: number;
  examples: string[];
  roots: string[];
  excludes: string[];
};

export type SyncSettingsSaved = SyncSettings & { purged: number };

export type RunSummary = {
  id: number;
  source: "schedule" | "web" | "cli";
  status: "running" | "succeeded" | "failed" | "abandoned";
  started_at: string;
  heartbeat_at: string;
  finished_at: string | null;
  counts: Record<string, number>;
  error: string | null;
};

export type HeldDeletions = {
  run_id: number;
  count: number;
  total: number;
  ratio: number;
  threshold: number;
  sample: string[];
  approved_at: string | null;
};

export type SyncStatus = {
  now: string;
  running: RunSummary | null;
  latest_run: RunSummary | null;
  last_success_at: string | null;
  queue: { queued: number; running: number; by_type: Record<string, number> };
  embed: { used: number; limit: number };
  last_backup_at: string | null;
  last_backup_name: string | null;
  held_deletions: HeldDeletions | null;
  skipped_formats: Record<string, number>;
  stale: { sync: boolean; backup: boolean };
};

export const SYNC_STATUS_KEY = ["sync", "status"] as const;
export const SYNC_SETTINGS_KEY = ["settings", "sync"] as const;

/** 브라우저가 Agent를 깨우는 주소(TRD 9.3, 10.2). `source`만 받는다. */
export const SYNC_RUN_URL = "chanjin-sync://run?source=web";

export async function getSyncStatus(): Promise<SyncStatus> {
  return (await apiFetch("/sync/status")).json() as Promise<SyncStatus>;
}

export async function getSyncSettings(): Promise<SyncSettings> {
  return (await apiFetch("/settings/sync")).json() as Promise<SyncSettings>;
}

export async function previewSyncSettings(input: SyncSettingsInput): Promise<SyncPreview> {
  const res = await apiFetch("/settings/sync/preview", { method: "POST", body: JSON.stringify(input) });
  return res.json() as Promise<SyncPreview>;
}

export async function saveSyncSettings(
  input: SyncSettingsInput & { expected_purge: number },
): Promise<SyncSettingsSaved> {
  const res = await apiFetch("/settings/sync", { method: "PUT", body: JSON.stringify(input) });
  return res.json() as Promise<SyncSettingsSaved>;
}

export async function applyHeldDeletions(runId: number): Promise<{ run_id: number; count: number; approved_at: string }> {
  const res = await apiFetch("/sync/held-deletions/apply", {
    method: "POST",
    body: JSON.stringify({ run_id: runId }),
  });
  return res.json() as Promise<{ run_id: number; count: number; approved_at: string }>;
}

/** "지금 동기화" 클릭 기록(서버는 로그만 남김). */
export async function recordRunRequest(): Promise<void> {
  await apiFetch("/sync/run-requests", { method: "POST" });
}
