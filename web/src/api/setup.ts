import { apiFetch } from "./client";

/** 첫 실행 체크리스트(F-18, TRD 8.3 `/setup/status`) */
export type SetupStep = {
  key: "user" | "token" | "agent" | "first_sync" | "documents";
  title: string;
  command: string | null;
  description: string;
  done: boolean;
  done_at: string | null;
};

export type SetupStatus = {
  user_exists: boolean;
  token_issued: boolean;
  agent_first_seen_at: string | null;
  first_sync_completed: boolean;
  first_sync_at: string | null;
  documents: number;
  steps: SetupStep[];
  completed: number;
  total: number;
  complete: boolean;
};

export const SETUP_STATUS_KEY = ["setup-status"] as const;

export async function getSetupStatus(): Promise<SetupStatus> {
  return (await (await apiFetch("/setup/status")).json()) as SetupStatus;
}
