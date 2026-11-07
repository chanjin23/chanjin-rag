/** 조건부 className 결합. shadcn/ui를 붙이면 clsx + tailwind-merge 버전으로 바꾼다. */
export function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}
