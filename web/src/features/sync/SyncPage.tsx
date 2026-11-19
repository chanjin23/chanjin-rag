import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import {
  applyHeldDeletions,
  getSyncSettings,
  getSyncStatus,
  type HeldDeletions,
  previewSyncSettings,
  recordRunRequest,
  type RunSummary,
  SYNC_RUN_URL,
  SYNC_SETTINGS_KEY,
  SYNC_STATUS_KEY,
  type SyncPreview,
  type SyncSettings,
  type SyncStatus,
  saveSyncSettings,
} from "@/api/sync";
import { isWindows } from "@/features/chat/format";
import { formatRelative } from "@/lib/time";
import { PURGE_NOTICE } from "@/features/documents/DocumentsPage";
import { cn } from "@/lib/cn";

/**
 * 동기화 화면 (TRD 9.1 `/sync`, 9.3 "지금 동기화", 12.4 경고 배지, F-01~F-03).
 * - 상태: 최근 실행, 대기열, 오늘 임베딩 사용량, 최근 백업, 보류된 삭제 판정, 건너뛴 형식
 * - 범위 편집: 저장 전에 미리보기(삭제될 문서 수·예시)를 확인 창으로 보여주고, 확인한 수로 저장
 * - 지금 동기화: chanjin-sync 링크 → 60초 비활성화, 2초 간격 조회, 30초 안에 새 실행이 없으면 CLI 안내
 */

/** 버튼 비활성화 시간(Agent의 `run` 반복 호출 제한 60초와 맞춤, TRD 10.2) */
export const RUN_COOLDOWN_MS = 60_000;
/** 이 시간 안에 새 실행이 생기지 않으면 CLI 안내 */
export const RUN_WAIT_MS = 30_000;
const POLL_MS = 2_000;

const RUN_STATUS_LABEL: Record<RunSummary["status"], string> = {
  running: "진행 중",
  succeeded: "성공",
  failed: "실패",
  abandoned: "중단됨",
};

const SOURCE_LABEL: Record<RunSummary["source"], string> = {
  schedule: "예약",
  web: "웹",
  cli: "CLI",
};

const COUNT_LABEL: Record<string, string> = {
  files: "파일",
  upload: "업로드",
  meta_only: "파일명만",
  deleted: "삭제",
  restored: "복원",
  moved: "이동",
  copied: "복사",
  unchanged: "변경 없음",
  upload_failed: "업로드 실패",
  scan_errors: "읽지 못한 폴더",
};

const JOB_LABEL: Record<string, string> = { extract: "추출", index: "색인", embed: "임베딩" };

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : "요청을 처리하지 못했습니다";
}

function Card({ title, children, testId }: { title: string; children: ReactNode; testId?: string }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3" data-testid={testId} aria-label={title}>
      <h2 className="text-xs font-medium text-slate-500">{title}</h2>
      <div className="mt-1 text-sm">{children}</div>
    </section>
  );
}

function StaleBadge({ children }: { children: ReactNode }) {
  return (
    <span role="status" className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900">
      {children}
    </span>
  );
}

function RunCounts({ run }: { run: RunSummary }) {
  const entries = Object.entries(run.counts).filter(([k]) => COUNT_LABEL[k]);
  if (entries.length === 0) return null;
  return (
    <p className="text-xs text-slate-500">
      {entries.map(([k, v]) => `${COUNT_LABEL[k]} ${v}`).join(" · ")}
    </p>
  );
}

function RunProgress({ run }: { run: RunSummary }) {
  return (
    <div role="status" data-testid="sync-progress" className="rounded-md bg-sky-50 px-3 py-2 text-sm text-sky-900">
      <p className="font-medium">
        <span className="mr-2 inline-block h-2 w-2 animate-pulse rounded-full bg-sky-500" aria-hidden />
        동기화 진행 중
      </p>
      <p className="text-xs">
        {SOURCE_LABEL[run.source]}에서 시작 · {formatRelative(run.started_at)} · 마지막 응답{" "}
        {formatRelative(run.heartbeat_at)}
      </p>
      <RunCounts run={run} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 지금 동기화
// ---------------------------------------------------------------------------

type Launch = { at: number; baselineRunId: number };

function SyncNow({
  status,
  launch,
  onLaunch,
}: {
  status: SyncStatus;
  launch: Launch | null;
  onLaunch: () => void;
}) {
  const [, setTick] = useState(0);
  const windows = isWindows();
  // 비활성화·대기 시간이 끝나면 다시 그린다(page.clock으로 E2E가 시간을 제어)
  useEffect(() => {
    if (!launch) return;
    const timers = [RUN_WAIT_MS, RUN_COOLDOWN_MS].map((ms) =>
      setTimeout(() => setTick((t) => t + 1), Math.max(0, launch.at + ms - Date.now())),
    );
    return () => timers.forEach(clearTimeout);
  }, [launch]);

  if (status.running) return <RunProgress run={status.running} />;

  const now = Date.now();
  const cooling = launch !== null && now - launch.at < RUN_COOLDOWN_MS;
  const newRun = launch !== null && status.latest_run !== null && status.latest_run.id > launch.baselineRunId;
  const waiting = launch !== null && !newRun && now - launch.at < RUN_WAIT_MS;
  const timedOut = launch !== null && !newRun && now - launch.at >= RUN_WAIT_MS;

  return (
    <div className="space-y-2">
      {windows ? (
        <button
          type="button"
          onClick={onLaunch}
          disabled={cooling}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          지금 동기화
        </button>
      ) : (
        <p className="text-sm text-slate-600">
          지금 동기화는 Windows PC에서 실행합니다. PC에서 <code className="font-mono">rag-agent sync</code>를 실행해도
          됩니다.
        </p>
      )}
      {waiting && (
        <p role="status" className="text-sm text-slate-600">
          Agent 실행을 기다리는 중입니다…
        </p>
      )}
      {newRun && status.latest_run && (
        <p role="status" className="text-sm text-slate-600">
          동기화 실행이 {RUN_STATUS_LABEL[status.latest_run.status]} 상태입니다.
        </p>
      )}
      {timedOut && (
        <div role="alert" data-testid="cli-hint" className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <p>Agent가 설치되지 않았거나 브라우저가 실행을 막았습니다.</p>
          <p>
            PC의 명령 창에서 <code className="rounded bg-white px-1 font-mono">rag-agent sync</code>를 실행하세요.
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 보류된 삭제 판정
// ---------------------------------------------------------------------------

function ConfirmDialog({
  title,
  children,
  confirmLabel,
  danger,
  pending,
  error,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  pending: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => cancelRef.current?.focus(), []);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        className="max-h-[90dvh] w-full max-w-lg overflow-y-auto rounded-lg bg-white p-4 shadow-lg"
        onKeyDown={(e) => e.key === "Escape" && onCancel()}
      >
        <h2 id="confirm-title" className="font-semibold">
          {title}
        </h2>
        <div className="mt-2 space-y-2 text-sm text-slate-700">{children}</div>
        {error && (
          <p role="alert" className="mt-2 text-sm text-red-700">
            {error}
          </p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded-md px-3 py-1.5 text-sm hover:bg-slate-100"
          >
            취소
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={pending}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm text-white disabled:opacity-50",
              danger ? "bg-red-600 hover:bg-red-700" : "bg-slate-900 hover:bg-slate-700",
            )}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

function HeldDeletionsPanel({ held }: { held: HeldDeletions }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const apply = useMutation({
    mutationFn: () => applyHeldDeletions(held.run_id),
    onSuccess: async () => {
      setConfirming(false);
      await queryClient.invalidateQueries({ queryKey: SYNC_STATUS_KEY });
    },
  });
  const percent = Math.round(held.ratio * 100);
  return (
    <section
      role="alert"
      aria-label="보류된 삭제 판정"
      className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950"
    >
      <h2 className="font-semibold">삭제 판정 보류</h2>
      <p className="mt-1">
        마지막 동기화에서 문서 {held.total}개 중 {held.count}개({percent}%)가 사라져, 스캔 오류일 수 있어 삭제 판정을
        보류했습니다(기준 {Math.round(held.threshold * 100)}%).
      </p>
      {held.sample.length > 0 && (
        <ul className="mt-1 list-inside list-disc font-mono text-[11px] text-amber-900">
          {held.sample.map((path) => (
            <li key={path} className="truncate" title={path}>
              {path}
            </li>
          ))}
        </ul>
      )}
      {held.approved_at ? (
        <p role="status" className="mt-2 font-medium">
          적용을 승인했습니다. 다음 동기화 때 삭제 판정을 적용합니다.
        </p>
      ) : (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className="mt-2 rounded-md border border-amber-400 bg-white px-3 py-1.5 hover:bg-amber-100"
        >
          삭제 판정 적용
        </button>
      )}
      {confirming && (
        <ConfirmDialog
          title="보류된 삭제 판정을 적용할까요?"
          confirmLabel="적용"
          pending={apply.isPending}
          error={apply.error ? errorMessage(apply.error) : null}
          onConfirm={() => apply.mutate()}
          onCancel={() => setConfirming(false)}
        >
          <p>
            다음 동기화에서도 파일이 없으면 문서 {held.count}개를 "로컬 삭제"로 표시합니다. 로컬 삭제 문서는 30일 뒤
            영구 삭제됩니다.
          </p>
          <p>폴더를 옮겼거나 외장 디스크가 빠져 있었다면 적용하지 마세요.</p>
        </ConfirmDialog>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// 범위 편집
// ---------------------------------------------------------------------------

function ListEditor({
  label,
  items,
  placeholder,
  onAdd,
  onRemove,
  validate,
}: {
  label: string;
  items: string[];
  placeholder: string;
  onAdd: (value: string) => void;
  onRemove: (value: string) => void;
  validate: (value: string) => string | null;
}) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const add = (e: FormEvent) => {
    e.preventDefault();
    const value = draft.trim();
    if (!value) return;
    const problem = validate(value);
    if (problem) {
      setError(problem);
      return;
    }
    onAdd(value);
    setDraft("");
    setError(null);
  };
  return (
    <div>
      <ul className="space-y-1" aria-label={label}>
        {items.map((item) => (
          <li key={item} className="flex items-center gap-2 rounded bg-slate-50 px-2 py-1">
            <span className="min-w-0 flex-1 truncate font-mono text-xs">{item}</span>
            <button
              type="button"
              onClick={() => onRemove(item)}
              aria-label={`${item} 삭제`}
              className="rounded px-1.5 text-xs text-slate-500 hover:bg-slate-200"
            >
              삭제
            </button>
          </li>
        ))}
      </ul>
      <form onSubmit={add} className="mt-1 flex gap-1">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={placeholder}
          aria-label={`${label} 추가`}
          className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 font-mono text-xs"
        />
        <button type="submit" className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-slate-100">
          추가
        </button>
      </form>
      {error && (
        <p role="alert" className="mt-1 text-xs text-red-700">
          {error}
        </p>
      )}
    </div>
  );
}

function isFixedPattern(value: string, fixed: string[]): boolean {
  const norm = value.trim().replaceAll("\\", "/").toLowerCase();
  return fixed.some((f) => norm === f || norm === f.replace(/\/$/, ""));
}

function ScopeEditor({ settings }: { settings: SyncSettings }) {
  const queryClient = useQueryClient();
  const fixed = settings.excludes.filter((e) => e.fixed).map((e) => e.pattern);
  const [roots, setRoots] = useState(settings.roots);
  const [excludes, setExcludes] = useState(settings.excludes.filter((e) => !e.fixed).map((e) => e.pattern));
  const [preview, setPreview] = useState<SyncPreview | null>(null);
  const [saved, setSaved] = useState<number | null>(null);

  const runPreview = useMutation({
    mutationFn: () => previewSyncSettings({ roots, excludes }),
    onSuccess: (data) => {
      setSaved(null);
      setPreview(data);
    },
  });
  const save = useMutation({
    mutationFn: (p: SyncPreview) =>
      saveSyncSettings({ roots: p.roots, excludes: p.excludes, expected_purge: p.count }),
    onSuccess: async (data) => {
      setPreview(null);
      setSaved(data.purged);
      setRoots(data.roots);
      setExcludes(data.excludes.filter((e) => !e.fixed).map((e) => e.pattern));
      queryClient.setQueryData(SYNC_SETTINGS_KEY, data);
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
    onError: (err) => {
      // 미리보기 뒤에 문서가 바뀌었으면 새 수로 다시 확인받는다
      if (err instanceof ApiError && err.code === "preview_outdated") runPreview.mutate();
    },
  });

  const dirty =
    JSON.stringify(roots) !== JSON.stringify(settings.roots) ||
    JSON.stringify(excludes) !== JSON.stringify(settings.excludes.filter((e) => !e.fixed).map((e) => e.pattern));

  return (
    <section aria-label="수집 범위" className="rounded-lg border border-slate-200 bg-white p-3">
      <h2 className="font-semibold">수집 범위</h2>
      <p className="mt-0.5 text-xs text-slate-500">
        루트는 <span className="font-mono">{settings.allowed_root}</span> 하위만 지정할 수 있습니다. 범위에서 빠지는
        문서는 저장할 때 서버에서 바로 영구 삭제합니다.
      </p>
      <div className="mt-3 grid gap-4 md:grid-cols-2">
        <div>
          <h3 className="mb-1 text-sm font-medium">수집 루트</h3>
          <ListEditor
            label="수집 루트"
            items={roots}
            placeholder={`${settings.allowed_root}\\notes`}
            onAdd={(v) => setRoots((r) => (r.includes(v) ? r : [...r, v]))}
            onRemove={(v) => setRoots((r) => r.filter((x) => x !== v))}
            validate={() => null}
          />
        </div>
        <div>
          <h3 className="mb-1 text-sm font-medium">제외 패턴</h3>
          <ul className="mb-1 space-y-1" aria-label="고정 제외">
            {fixed.map((pattern) => (
              <li
                key={pattern}
                data-testid="fixed-exclude"
                className="flex items-center gap-2 rounded bg-slate-100 px-2 py-1 text-slate-600"
              >
                <span className="min-w-0 flex-1 truncate font-mono text-xs">{pattern}</span>
                <span className="rounded bg-slate-200 px-1.5 text-[11px]" title="코드에 있는 제외라 지울 수 없습니다">
                  고정 · 편집 불가
                </span>
              </li>
            ))}
          </ul>
          <ListEditor
            label="제외 패턴"
            items={excludes}
            placeholder="*.bak 또는 private/"
            onAdd={(v) => setExcludes((x) => (x.includes(v) ? x : [...x, v]))}
            onRemove={(v) => setExcludes((x) => x.filter((p) => p !== v))}
            validate={(v) => (isFixedPattern(v, fixed) ? "고정 제외는 이미 적용되어 있어 편집할 수 없습니다" : null)}
          />
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => runPreview.mutate()}
          disabled={!dirty || runPreview.isPending}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-50"
        >
          저장
        </button>
        {dirty && (
          <button
            type="button"
            onClick={() => {
              setRoots(settings.roots);
              setExcludes(settings.excludes.filter((e) => !e.fixed).map((e) => e.pattern));
            }}
            className="rounded-md px-3 py-1.5 text-sm hover:bg-slate-100"
          >
            되돌리기
          </button>
        )}
        {saved !== null && (
          <p role="status" className="text-sm text-emerald-700">
            저장했습니다. {saved > 0 ? `범위에서 빠진 문서 ${saved}개를 삭제했습니다.` : "삭제된 문서는 없습니다."}
          </p>
        )}
        {runPreview.error && (
          <p role="alert" className="text-sm text-red-700">
            {errorMessage(runPreview.error)}
          </p>
        )}
      </div>
      {preview && (
        <ConfirmDialog
          title="수집 범위를 저장할까요?"
          confirmLabel={preview.count > 0 ? `${preview.count}개 삭제하고 저장` : "저장"}
          danger={preview.count > 0}
          pending={save.isPending}
          error={save.error ? errorMessage(save.error) : null}
          onConfirm={() => save.mutate(preview)}
          onCancel={() => setPreview(null)}
        >
          {preview.count > 0 ? (
            <>
              <p className="font-medium text-red-700" data-testid="purge-count">
                범위에서 빠지는 문서 {preview.count}개를 서버에서 영구 삭제합니다.
              </p>
              <p className="text-xs text-slate-500">예시(최대 10개)</p>
              <ul className="list-inside list-disc font-mono text-[11px]" aria-label="삭제될 문서 예시">
                {preview.examples.map((path) => (
                  <li key={path} className="truncate" title={path}>
                    {path}
                  </li>
                ))}
              </ul>
              <p>예전 답변의 인용 구절은 "삭제된 문서"로 가립니다. 내 PC의 원본 파일은 지우지 않습니다.</p>
            </>
          ) : (
            <p data-testid="purge-count">범위에서 빠지는 문서가 없습니다. 삭제되는 문서 0개.</p>
          )}
          <p className="rounded bg-amber-50 px-2 py-1 text-amber-900">{PURGE_NOTICE}</p>
        </ConfirmDialog>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// 화면
// ---------------------------------------------------------------------------

export function SyncPage() {
  const queryClient = useQueryClient();
  const [launch, setLaunch] = useState<Launch | null>(null);
  const linkRef = useRef<HTMLAnchorElement>(null);

  const status = useQuery({
    queryKey: SYNC_STATUS_KEY,
    queryFn: getSyncStatus,
    // 실행 중이거나 "지금 동기화"를 누른 뒤 30초 동안은 2초 간격으로 조회(TRD 9.3)
    refetchInterval: (query) => {
      const data = query.state.data;
      if (data?.running) return POLL_MS;
      if (launch && Date.now() - launch.at < RUN_WAIT_MS) return POLL_MS;
      return false;
    },
  });
  const settings = useQuery({ queryKey: SYNC_SETTINGS_KEY, queryFn: getSyncSettings });

  const onLaunch = () => {
    setLaunch({ at: Date.now(), baselineRunId: status.data?.latest_run?.id ?? 0 });
    linkRef.current?.click();
    void recordRunRequest().catch(() => undefined);
    void queryClient.invalidateQueries({ queryKey: SYNC_STATUS_KEY });
  };

  const s = status.data;
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold">동기화</h1>
          {s?.stale.sync && <StaleBadge>마지막 동기화가 하루 넘음</StaleBadge>}
          {s?.stale.backup && <StaleBadge>마지막 백업이 하루 넘음</StaleBadge>}
        </div>
        {/* 브라우저가 Agent를 깨우는 링크(TRD 9.3). 숨긴 <a>를 눌러 연다. */}
        <a ref={linkRef} href={SYNC_RUN_URL} hidden data-testid="sync-run-link" aria-hidden>
          chanjin-sync
        </a>

        {status.isPending && <p className="text-sm text-slate-500">불러오는 중…</p>}
        {status.error && (
          <p role="alert" className="text-sm text-red-700">
            {errorMessage(status.error)}
          </p>
        )}
        {s && (
          <>
            <SyncNow status={s} launch={launch} onLaunch={onLaunch} />
            {s.held_deletions && <HeldDeletionsPanel held={s.held_deletions} />}
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Card title="최근 실행" testId="latest-run">
                {s.latest_run ? (
                  <>
                    <p>
                      {RUN_STATUS_LABEL[s.latest_run.status]} · {SOURCE_LABEL[s.latest_run.source]} ·{" "}
                      {formatRelative(s.latest_run.started_at)}
                    </p>
                    <RunCounts run={s.latest_run} />
                    {s.latest_run.error && <p className="text-xs text-red-700">{s.latest_run.error}</p>}
                  </>
                ) : (
                  <p className="text-slate-500">실행 기록 없음</p>
                )}
                <p className="text-xs text-slate-500">
                  마지막 성공: {s.last_success_at ? formatRelative(s.last_success_at) : "없음"}
                </p>
              </Card>
              <Card title="대기열" testId="queue">
                <p>
                  대기 {s.queue.queued} · 처리 중 {s.queue.running}
                </p>
                <p className="text-xs text-slate-500">
                  {Object.entries(s.queue.by_type)
                    .map(([k, v]) => `${JOB_LABEL[k] ?? k} ${v}`)
                    .join(" · ")}
                </p>
              </Card>
              <Card title="오늘 임베딩 사용량" testId="embed-usage">
                <p>
                  {s.embed.used}/{s.embed.limit}
                </p>
                <p className="text-xs text-slate-500">하루 파일 한도, 0시(한국 시간)에 초기화</p>
              </Card>
              <Card title="최근 백업" testId="last-backup">
                <p>{s.last_backup_at ? formatRelative(s.last_backup_at) : "백업 기록 없음"}</p>
                {s.last_backup_name && <p className="text-xs text-slate-500">{s.last_backup_name}</p>}
                <p className="text-xs text-slate-500">PC에는 동기화 때 내려받아 최근 7개 보관</p>
              </Card>
            </div>
            <Card title="건너뛴 형식" testId="skipped-formats">
              {Object.keys(s.skipped_formats).length > 0 ? (
                <p>
                  {Object.entries(s.skipped_formats)
                    .sort((a, b) => b[1] - a[1])
                    .map(([ext, n]) => `${ext} ${n}개`)
                    .join(" · ")}
                </p>
              ) : (
                <p className="text-slate-500">없음</p>
              )}
              <p className="text-xs text-slate-500">지원하지 않는 형식이라 동기화하지 않은 파일(마지막 성공 실행)</p>
            </Card>
          </>
        )}
        {settings.data && <ScopeEditor settings={settings.data} />}
        {settings.error && (
          <p role="alert" className="text-sm text-red-700">
            {errorMessage(settings.error)}
          </p>
        )}
      </div>
    </div>
  );
}
