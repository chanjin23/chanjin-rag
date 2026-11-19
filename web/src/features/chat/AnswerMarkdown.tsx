import { memo } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * 답변 렌더링 (TRD 9.2 "답변 렌더링", PRD 7장).
 * - react-markdown + remark-gfm. 원시 HTML은 렌더링하지 않는다(skipHtml).
 * - 이미지는 불러오지 않고 대체 텍스트만, 링크는 주소를 텍스트로만 보여준다. 모델이 만든 외부 주소로
 *   브라우저가 요청을 보내 문서 내용이 새는 것을 막는다.
 * - 본문의 `[n]`·`[1, 2]`는 인용 칩(버튼)으로 바꾼다. 코드(인라인·블록) 안은 mdast에서 `code`/`inlineCode`
 *   노드라 text 노드만 바꾸는 이 플러그인이 건드리지 않는다. 출처 목록에 없는 번호는 글자 그대로 둔다.
 */

const CITATION_RE = /\[(\d+(?:\s*,\s*\d+)*)\]/g;

type MdNode = {
  type: string;
  value?: string;
  children?: MdNode[];
  data?: { hName?: string; hProperties?: Record<string, unknown> };
};

function splitCitations(value: string, valid: ReadonlySet<number>): MdNode[] | null {
  const out: MdNode[] = [];
  let last = 0;
  let changed = false;
  for (const m of value.matchAll(CITATION_RE)) {
    const numbers = (m[1] ?? "").split(",").map((p) => Number(p.trim()));
    if (!numbers.every((n) => valid.has(n))) continue;
    changed = true;
    if (m.index > last) out.push({ type: "text", value: value.slice(last, m.index) });
    for (const n of numbers) {
      out.push({
        type: "citation",
        children: [{ type: "text", value: `[${n}]` }],
        data: { hName: "cite", hProperties: { dataN: String(n) } },
      });
    }
    last = m.index + m[0].length;
  }
  if (!changed) return null;
  if (last < value.length) out.push({ type: "text", value: value.slice(last) });
  return out;
}

function remarkCitations(valid: ReadonlySet<number>) {
  const walk = (node: MdNode) => {
    if (!node.children) return;
    const next: MdNode[] = [];
    for (const child of node.children) {
      if (child.type === "text" && child.value) {
        const parts = splitCitations(child.value, valid);
        if (parts) {
          next.push(...parts);
          continue;
        }
      }
      // 링크 글자 안의 인용은 칩으로 바꾸지 않는다(링크는 어차피 텍스트로 보여줌)
      if (child.type !== "link" && child.type !== "linkReference") walk(child);
      next.push(child);
    }
    node.children = next;
  };
  return () => (tree: MdNode) => walk(tree);
}

type Props = {
  content: string;
  /** 출처 목록에 있는 인용 번호 */
  citationNumbers: number[];
  onCite: (n: number) => void;
};

export const AnswerMarkdown = memo(function AnswerMarkdown({ content, citationNumbers, onCite }: Props) {
  const valid = new Set(citationNumbers);
  const components: Components = {
    cite: ({ node }) => {
      const n = Number(node?.properties?.dataN);
      return (
        <button
          type="button"
          onClick={() => onCite(n)}
          aria-label={`인용 ${n}`}
          data-citation={n}
          className="mx-0.5 inline-flex items-center rounded bg-sky-100 px-1 align-baseline text-xs font-medium text-sky-800 hover:bg-sky-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500"
        >
          [{n}]
        </button>
      );
    },
    // 이미지는 불러오지 않는다: 대체 텍스트만
    img: ({ alt }) => (
      <span className="text-slate-500" data-blocked-image="">
        [이미지: {alt || "설명 없음"}]
      </span>
    ),
    // 링크는 이동하지 않는 텍스트로: 글자와 주소
    a: ({ children, href }) => (
      <span data-blocked-link="">
        {children}
        {href && String(children) !== href && (
          <span className="text-slate-500"> ({href})</span>
        )}
      </span>
    ),
    pre: ({ children }) => (
      <pre className="my-2 overflow-x-auto rounded-lg bg-slate-100 p-3 text-xs">{children}</pre>
    ),
    code: ({ children, className }) => (
      <code className={className ?? "rounded bg-slate-100 px-1 text-[0.85em]"}>{children}</code>
    ),
    p: ({ children }) => <p className="my-1.5 first:mt-0 last:mb-0">{children}</p>,
    ul: ({ children }) => <ul className="my-1.5 list-disc pl-5">{children}</ul>,
    ol: ({ children }) => <ol className="my-1.5 list-decimal pl-5">{children}</ol>,
    table: ({ children }) => (
      <div className="my-2 overflow-x-auto">
        <table className="border-collapse text-xs [&_td]:border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:px-2 [&_th]:py-1">
          {children}
        </table>
      </div>
    ),
  };
  return (
    <Markdown
      remarkPlugins={[remarkGfm, remarkCitations(valid)]}
      skipHtml
      components={components}
    >
      {content}
    </Markdown>
  );
});
