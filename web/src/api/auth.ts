import { apiFetch } from "@/api/client";

/** 인증 API (TRD 8.1). 백엔드 app/api/auth.py와 맞춘다. */
export type User = { id: number; username: string };

export const ME_QUERY_KEY = ["auth", "me"] as const;

export async function fetchMe(): Promise<User> {
  return (await (await apiFetch("/auth/me")).json()) as User;
}

/** 실패: 401 `invalid_credentials`, 잠금 429 `login_locked`(detail.retry_after_s). */
export async function login(username: string, password: string): Promise<User> {
  const res = await apiFetch("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  return (await res.json()) as User;
}

export async function logout(): Promise<void> {
  await apiFetch("/auth/logout", { method: "POST" });
}
