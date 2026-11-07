/**
 * API 호출 공통 규칙 (TRD 9.2, 11장)
 * - 세션 쿠키를 보내도록 credentials: "include"
 * - GET이 아닌 요청에는 CSRF 방어용 X-Requested-With 헤더
 * - 오류 응답 본문 `{"error": {"code", "message", ...}}`(TRD 8장)을 ApiError로 바꾼다
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly detail: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

/** 세션이 없거나 만료된 401인지. 웹은 이때 입력을 보존하고 로그인 화면으로 보낸다(TRD 8.3). */
export function isUnauthenticated(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401 && err.code === "unauthenticated";
}

async function toApiError(res: Response, label: string): Promise<ApiError> {
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    if (body.error?.code) {
      const { code, message, ...detail } = body.error;
      return new ApiError(res.status, code, message ?? label, detail);
    }
  } catch {
    // JSON이 아닌 응답(프록시 오류 등)
  }
  return new ApiError(res.status, "http_error", `${label} 실패 (${res.status})`);
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (method !== "GET") headers.set("X-Requested-With", "fetch");
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  const res = await fetch(`/api${path}`, { ...init, method, headers, credentials: "include" });
  if (!res.ok) throw await toApiError(res, `${method} /api${path}`);
  return res;
}
