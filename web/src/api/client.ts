/**
 * API 호출 공통 규칙 (TRD 9.2, 11장)
 * - 세션 쿠키를 보내도록 credentials: "include"
 * - GET이 아닌 요청에는 CSRF 방어용 X-Requested-With 헤더
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (method !== "GET") headers.set("X-Requested-With", "fetch");
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  const res = await fetch(`/api${path}`, { ...init, method, headers, credentials: "include" });
  if (!res.ok) {
    throw new ApiError(res.status, `${method} /api${path} 실패 (${res.status})`);
  }
  return res;
}
