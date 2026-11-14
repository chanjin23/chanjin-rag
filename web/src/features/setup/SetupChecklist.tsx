import type { SetupStatus } from "@/api/setup";
import { cn } from "@/lib/cn";
import { formatRelativeDateTime } from "@/lib/time";

/**
 * 첫 실행 체크리스트(F-18, TRD 9.1). 문서가 0개인 채팅 화면에 빈 안내 대신 보여준다.
 * 단계마다 할 명령과 완료 표시(완료 시각은 한국 시간, 하루 이내는 상대 시각).
 */
export function SetupChecklist({ status }: { status: SetupStatus }) {
  return (
    <section
      aria-labelledby="setup-checklist-title"
      data-testid="setup-checklist"
      className="mx-auto mt-6 max-w-xl rounded-xl border border-slate-200 bg-white p-5"
    >
      <h2 id="setup-checklist-title" className="text-base font-semibold">
        첫 실행 체크리스트
      </h2>
      <p className="mt-1 text-sm text-slate-600">
        아직 수집된 문서가 없습니다. 아래 단계를 마치면 내 문서에 대해 질문할 수 있습니다.{" "}
        <span data-testid="setup-progress">
          ({status.completed}/{status.total} 완료)
        </span>
      </p>
      <ol className="mt-4 space-y-3">
        {status.steps.map((step, i) => (
          <li
            key={step.key}
            data-testid={`setup-step-${step.key}`}
            data-done={step.done ? "true" : "false"}
            className="flex gap-3"
          >
            <span
              aria-hidden
              className={cn(
                "mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold",
                step.done ? "bg-emerald-600 text-white" : "border border-slate-300 text-slate-500",
              )}
            >
              {step.done ? "✓" : i + 1}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">
                {step.title}{" "}
                <span
                  className={cn(
                    "ml-1 rounded px-1.5 py-0.5 text-xs font-normal",
                    step.done ? "bg-emerald-50 text-emerald-800" : "bg-slate-100 text-slate-600",
                  )}
                >
                  {step.done ? "완료" : "할 일"}
                  {step.done && step.done_at ? ` · ${formatRelativeDateTime(step.done_at)}` : ""}
                </span>
              </p>
              <p className="text-xs text-slate-500">{step.description}</p>
              {step.command && (
                <code className="mt-1 block overflow-x-auto whitespace-pre rounded bg-slate-900 px-2 py-1 text-xs text-slate-100">
                  {step.command}
                </code>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
