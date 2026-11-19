import { useSyncExternalStore } from "react";

/** CSS 미디어 쿼리 일치 여부. 창 크기가 바뀌면 다시 그린다. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}

/** 너비 768px 미만(TRD 9.1: 대화 목록·출처 패널을 서랍으로 접는다) */
export const NARROW_QUERY = "(max-width: 767.98px)";
