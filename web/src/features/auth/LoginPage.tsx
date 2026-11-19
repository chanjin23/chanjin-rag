import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { login, ME_QUERY_KEY } from "@/api/auth";
import { ApiError } from "@/api/client";
import { safeNext } from "./redirect";

/** 남은 초를 "14분 59초"처럼 보여준다. */
function formatRemaining(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}분 ${s}초` : `${s}초`;
}

/** 로그인 화면 (TRD 9.1 `/login`): 잠금 남은 시간, 비밀번호 분실 안내. */
export function LoginPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));
  const expired = params.get("reason") === "expired";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [lockedUntil, setLockedUntil] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const remaining = lockedUntil ? Math.max(0, Math.ceil((lockedUntil - now) / 1000)) : 0;
  const locked = remaining > 0;

  useEffect(() => {
    if (!lockedUntil) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [lockedUntil]);

  const mutation = useMutation({
    mutationFn: () => login(username.trim(), password),
    onSuccess: (user) => {
      queryClient.setQueryData(ME_QUERY_KEY, user);
      navigate(next, { replace: true });
    },
    onError: (err) => {
      setPassword("");
      if (err instanceof ApiError && err.code === "login_locked") {
        const seconds = Number(err.detail.retry_after_s ?? 0);
        setNow(Date.now());
        setLockedUntil(Date.now() + seconds * 1000);
        setError(null);
      } else if (err instanceof ApiError && err.code === "invalid_credentials") {
        setError("아이디 또는 비밀번호가 올바르지 않습니다.");
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password || locked || mutation.isPending) return;
    setError(null);
    mutation.mutate();
  };

  return (
    <div className="flex min-h-dvh items-center justify-center bg-slate-50 px-4 text-slate-900">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm"
      >
        <h1 className="text-lg font-semibold">chanjin-rag 로그인</h1>

        {expired && !error && !locked && (
          <p role="status" className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800">
            로그인이 만료되었습니다. 다시 로그인하면 작성하던 질문을 이어서 보낼 수 있습니다.
          </p>
        )}

        <label className="block space-y-1 text-sm">
          <span>아이디</span>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            className="w-full rounded-lg border border-slate-300 px-3 py-2 outline-none focus:border-slate-500"
          />
        </label>
        <label className="block space-y-1 text-sm">
          <span>비밀번호</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            className="w-full rounded-lg border border-slate-300 px-3 py-2 outline-none focus:border-slate-500"
          />
        </label>

        {error && (
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
        )}
        {locked && (
          <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
            비밀번호를 여러 번 틀려 로그인이 잠겼습니다.{" "}
            <strong>{formatRemaining(remaining)}</strong> 뒤에 다시 시도할 수 있습니다.
          </p>
        )}

        <button
          type="submit"
          disabled={locked || mutation.isPending || !username.trim() || !password}
          className="w-full rounded-lg bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-40"
        >
          {mutation.isPending ? "로그인 중…" : "로그인"}
        </button>

        <p className="text-xs leading-relaxed text-slate-500">
          비밀번호를 잊었으면 서버에서{" "}
          <code className="rounded bg-slate-100 px-1">rag-backend reset-password &lt;아이디&gt;</code>
          를 실행하세요. 잠금도 함께 풀립니다.
        </p>
      </form>
    </div>
  );
}
