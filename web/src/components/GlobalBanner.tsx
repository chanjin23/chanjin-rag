import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { getHealth, HEALTH_KEY, type HealthIssue, type HealthIssueCode } from "@/api/health";
import { issueKey, pruneDismissed } from "@/lib/banner";
import { formatRelativeDateTime } from "@/lib/time";

/**
 * 전역 배너(TRD 9.1 공통 화면 상태, 12.4). 모든 화면 위에 `/health`의 이상을 띄운다.
 * - 대상: Provider 인증 오류(`provider_auth`), Worker 하트비트 끊김·기록 없음, 디스크 부족, DB 연결 실패
 * - 30초마다 다시 확인한다. 채팅 답변이 끝날 때도 다시 읽는다(인증 오류를 바로 보이게).
 * - 닫은 배너는 이 탭(sessionStorage)에서 숨기되, 이상의 내용(코드·since·level)이 바뀌거나
 *   이상이 사라졌다가 다시 생기면 다시 보여준다. 같은 장애가 이어지는 동안(연속 실패)은 since가
 *   그대로라 다시 뜨지 않는다.
 */

const REFRESH_MS = 30_000;
const STORAGE_KEY = "rag.banner.dismissed";

const TITLES: Record<HealthIssueCode, string> = {
  provider_auth: "모델 API 인증 오류",
  worker_stale: "Worker 응답 없음",
  worker_missing: "Worker 실행 안 됨",
  disk_low: "디스크 공간 부족",
  db_unavailable: "DB 연결 실패",
};

function loadDismissed(): string[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

function saveDismissed(keys: string[]): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(keys));
  } catch {
    // 저장소를 못 쓰면 이 화면 동안만 기억한다
  }
}

export function GlobalBanner() {
  const health = useQuery({
    queryKey: HEALTH_KEY,
    queryFn: getHealth,
    refetchInterval: REFRESH_MS,
    retry: false,
  });
  const [dismissed, setDismissed] = useState<string[]>(loadDismissed);
  const issues = health.data?.issues ?? [];

  // 지금 없는 이상의 "닫음" 기록은 지운다 → 같은 이상이 다시 생기면 다시 보인다
  const currentKeys = issues.map(issueKey).join("\n");
  useEffect(() => {
    if (!health.data) return;
    const live = currentKeys ? currentKeys.split("\n") : [];
    setDismissed((prev) => {
      const next = pruneDismissed(prev, live);
      if (next.length === prev.length) return prev;
      saveDismissed(next);
      return next;
    });
  }, [currentKeys, health.data]);

  const visible = issues.filter((i) => !dismissed.includes(issueKey(i)));
  if (visible.length === 0) return null;

  const dismiss = (issue: HealthIssue) => {
    const next = [...dismissed, issueKey(issue)];
    setDismissed(next);
    saveDismissed(next);
  };

  return (
    <div aria-label="시스템 알림" role="region" className="border-b border-red-200 bg-red-50">
      {visible.map((issue) => (
        <div
          key={issueKey(issue)}
          role="alert"
          aria-label={TITLES[issue.code]}
          data-testid={`banner-${issue.code}`}
          className="flex items-start gap-3 px-4 py-2 text-sm text-red-900"
        >
          <p className="flex-1">
            <strong className="mr-2 font-semibold">{TITLES[issue.code]}</strong>
            {issue.message}
            {issue.since && (
              <span className="ml-2 text-xs text-red-700">({formatRelativeDateTime(issue.since)})</span>
            )}
          </p>
          <button
            type="button"
            onClick={() => dismiss(issue)}
            aria-label={`${TITLES[issue.code]} 알림 닫기`}
            className="rounded px-2 text-red-700 hover:bg-red-100"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
