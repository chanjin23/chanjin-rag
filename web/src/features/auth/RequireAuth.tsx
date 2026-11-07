import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";
import { fetchMe, ME_QUERY_KEY } from "@/api/auth";
import { isUnauthenticated } from "@/api/client";
import { loginPath } from "./redirect";

/** 로그인해야 볼 수 있는 화면. 세션이 없으면 지금 경로를 `next`로 붙여 로그인 화면으로 보낸다. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation();
  const me = useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: fetchMe,
    retry: (count, err) => !isUnauthenticated(err) && count < 2,
    staleTime: 60_000,
  });

  if (me.isPending) {
    return <p className="p-8 text-center text-sm text-slate-500">로그인 상태 확인 중…</p>;
  }
  if (me.isError) {
    if (isUnauthenticated(me.error)) {
      const next = `${location.pathname}${location.search}`;
      return <Navigate to={loginPath(next)} replace />;
    }
    return (
      <p role="alert" className="p-8 text-center text-sm text-red-600">
        서버에 연결하지 못했습니다: {me.error.message}
      </p>
    );
  }
  return children;
}
