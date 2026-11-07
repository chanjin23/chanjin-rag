type Props = { title: string; description: string };

/** 아직 구현하지 않은 화면의 자리 표시. */
export function Placeholder({ title, description }: Props) {
  return (
    <div className="mx-auto max-w-3xl p-8">
      <h1 className="text-xl font-semibold">{title}</h1>
      <p className="mt-2 text-sm text-slate-600">{description}</p>
    </div>
  );
}
