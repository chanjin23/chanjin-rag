/** 로그인 화면으로 보낼 때 돌아올 화면(`next`)을 붙인다. 같은 사이트 경로만 허용한다. */
export function loginPath(next: string, reason?: "expired"): string {
  const params = new URLSearchParams({ next });
  if (reason) params.set("reason", reason);
  return `/login?${params.toString()}`;
}

export function safeNext(next: string | null): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/login")) {
    return "/";
  }
  return next;
}
