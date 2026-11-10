import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { type Feedback, type FeedbackReason, saveFeedback } from "@/api/conversations";
import { cn } from "@/lib/cn";

/**
 * 답변 피드백 (F-12): 좋아요·싫어요, 싫어요 사유("틀림", "실제로는 문서에 있음", "기타")와 메모.
 * 다시 누르면 덮어쓴다(`PUT /messages/{id}/feedback`).
 */

const REASONS: { value: FeedbackReason; label: string }[] = [
  { value: "wrong", label: "틀림" },
  { value: "not_found_wrong", label: "실제로는 문서에 있음" },
  { value: "other", label: "기타" },
];

type Props = {
  messageId: number;
  feedback: Feedback | null | undefined;
  onSaved: (feedback: Feedback) => void;
};

export function FeedbackBar({ messageId, feedback, onSaved }: Props) {
  const [formOpen, setFormOpen] = useState(false);
  const [reason, setReason] = useState<FeedbackReason | null>(feedback?.reason ?? null);
  const [note, setNote] = useState(feedback?.note ?? "");
  const [notice, setNotice] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (next: Feedback) => saveFeedback(messageId, next),
    onSuccess: (saved) => {
      onSaved(saved);
      setFormOpen(false);
      setNotice("피드백을 저장했습니다");
    },
    onError: () => setNotice("피드백을 저장하지 못했습니다"),
  });

  const like = () => {
    setNotice(null);
    setFormOpen(false);
    save.mutate({ rating: 1, reason: null, note: null });
  };

  const submitDislike = (e: FormEvent) => {
    e.preventDefault();
    save.mutate({ rating: -1, reason, note: note.trim() || null });
  };

  const rating = feedback?.rating;
  return (
    <div className="mt-2 border-t border-slate-100 pt-2 text-xs">
      <div className="flex items-center gap-1" role="group" aria-label="답변 피드백">
        <button
          type="button"
          aria-pressed={rating === 1}
          disabled={save.isPending}
          onClick={like}
          className={cn(
            "rounded px-2 py-0.5",
            rating === 1 ? "bg-emerald-100 text-emerald-800" : "text-slate-500 hover:bg-slate-100",
          )}
        >
          좋아요
        </button>
        <button
          type="button"
          aria-pressed={rating === -1}
          aria-expanded={formOpen}
          disabled={save.isPending}
          onClick={() => {
            setNotice(null);
            setFormOpen((v) => !v);
          }}
          className={cn(
            "rounded px-2 py-0.5",
            rating === -1 ? "bg-rose-100 text-rose-800" : "text-slate-500 hover:bg-slate-100",
          )}
        >
          싫어요
        </button>
        {notice && (
          <span aria-live="polite" className="ml-1 text-slate-500">
            {notice}
          </span>
        )}
      </div>
      {formOpen && (
        <form onSubmit={submitDislike} className="mt-2 space-y-2 rounded-lg bg-slate-50 p-2" aria-label="싫어요 사유">
          <fieldset className="flex flex-wrap gap-3">
            <legend className="sr-only">사유</legend>
            {REASONS.map((r) => (
              <label key={r.value} className="flex items-center gap-1">
                <input
                  type="radio"
                  name={`reason-${messageId}`}
                  value={r.value}
                  checked={reason === r.value}
                  onChange={() => setReason(r.value)}
                />
                {r.label}
              </label>
            ))}
          </fieldset>
          <textarea
            aria-label="메모"
            placeholder="메모(선택): 어디가 틀렸는지, 실제로 어느 문서에 있는지"
            value={note}
            maxLength={2000}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            className="w-full resize-none rounded border border-slate-300 bg-white px-2 py-1 outline-none focus:border-slate-500"
          />
          <div className="flex gap-1">
            <button
              type="submit"
              disabled={save.isPending}
              className="rounded bg-slate-900 px-2 py-0.5 text-white disabled:opacity-40"
            >
              피드백 보내기
            </button>
            <button type="button" onClick={() => setFormOpen(false)} className="rounded px-2 py-0.5 hover:bg-slate-200">
              취소
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
