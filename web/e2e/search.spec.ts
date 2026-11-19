import { spawnSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { backendLogMark, findBackendLogs, waitForBackendLog } from "./backendLog";
import { BACKEND_LOG, TEST_DATABASE_URL, UPLOAD_TMP_DIR } from "./env";

/**
 * T09 Hybrid 검색: `rag-backend ingest-local`로 실제 수집 경로(파싱 → 청킹 → Kiwi → Fake 임베딩)를
 * 거친 문서를 `rag-backend search`로 찾는다. 테스트 DB(ParadeDB)의 pgvector + pg_search SQL 하나에서
 * RRF로 합친 결과, 필터, 검색 범위(coverage)를 CLI 출력과 백엔드 로그(`search.completed`)로 확인한다.
 * 채팅 화면 연결은 질의 그래프 작업(T10 이후)에서 하므로 화면 대신 CLI로 확인한다.
 * 테스트 DB는 E2E 실행 사이에 남으므로 실행마다 새 폴더를 만들고 디렉터리 필터로 범위를 좁힌다.
 */
const ROOT = path.resolve(import.meta.dirname, ".tmp", "search-root");

function backend(args: string[]) {
  return spawnSync("uv", ["run", "rag-backend", ...args], {
    cwd: path.resolve(import.meta.dirname, "..", "..", "backend"),
    encoding: "utf-8",
    env: {
      ...process.env,
      APP_ENV: "test",
      PROVIDER: "fake",
      DATABASE_URL: TEST_DATABASE_URL,
      LOG_FORMAT: "json",
      LOG_FILE: BACKEND_LOG,
      SYNC_ALLOWED_ROOT: ROOT,
      UPLOAD_TMP_DIR,
      PYTHONIOENCODING: "utf-8",
    },
  });
}

type SearchOutput = {
  coverage: {
    bm25_docs: number;
    vector_docs: number;
    pending_docs: number;
    total_docs: number;
    filters_applied: Record<string, unknown>;
  };
  latency_ms: Record<string, number>;
  vector_used: boolean;
  hits: {
    chunk_id: number;
    document_id: number;
    path: string;
    rank: number;
    rrf: number;
    vector_rank: number | null;
    bm25_rank: number | null;
    locally_deleted: boolean;
    snippet: string;
  }[];
};

function search(args: string[]): SearchOutput {
  const res = backend(["search", ...args, "--json"]);
  expect(res.status, res.stderr).toBe(0);
  const lines = res.stdout.trim().split("\n");
  return JSON.parse(lines[lines.length - 1]) as SearchOutput;
}

const names = (out: SearchOutput) => out.hits.map((h) => path.basename(h.path));

test("ingest-local로 넣은 문서를 Hybrid 검색으로 찾고 필터와 검색 범위를 적용한다", async () => {
  test.setTimeout(120_000);
  rmSync(ROOT, { recursive: true, force: true });
  const stamp = `${Date.now()}`;
  const folder = path.join(ROOT, `docs-${stamp}`);
  mkdirSync(path.join(folder, "sub"), { recursive: true });
  writeFileSync(path.join(folder, "계약.md"), "# 계약 조건\n\n위약금은 계약 금액의 10%로 한다.\n");
  writeFileSync(path.join(folder, "sub", "메모.txt"), "위약금 관련 메모: 다음 주에 확인.\n");
  writeFileSync(path.join(folder, "일정.md"), "# 일정\n\n배포는 금요일에 한다.\n");

  const ingest = backend(["ingest-local", folder]);
  expect(ingest.status, ingest.stderr).toBe(0);
  expect(ingest.stdout).toContain("접수: 새 버전 3");

  // 1) 디렉터리 필터: 이번 실행의 폴더 문서만. 키워드가 맞는 두 문서는 BM25와 벡터 양쪽에서,
  //    키워드가 없는 문서는 벡터로만 들어와 RRF로 합쳐진다.
  let since = backendLogMark();
  const all = search(["위약금", "--dir", folder]);
  expect(all.vector_used).toBe(true);
  expect(all.coverage).toEqual({
    bm25_docs: 3,
    vector_docs: 3,
    pending_docs: 0,
    total_docs: 3,
    filters_applied: { directory: folder },
  });
  expect(new Set(names(all).slice(0, 2))).toEqual(new Set(["계약.md", "메모.txt"]));
  const keywordHits = all.hits.filter((h) => h.bm25_rank !== null).map((h) => path.basename(h.path));
  expect(new Set(keywordHits)).toEqual(new Set(["계약.md", "메모.txt"]));
  const vectorOnly = all.hits.find((h) => path.basename(h.path) === "일정.md");
  expect(vectorOnly?.bm25_rank).toBeNull();
  expect(vectorOnly?.vector_rank).not.toBeNull();
  for (const hit of all.hits) {
    const expected = [hit.vector_rank, hit.bm25_rank]
      .filter((r): r is number => r !== null)
      .reduce((sum, r) => sum + 1 / (60 + r), 0);
    expect(hit.rrf).toBeCloseTo(expected, 5);
    expect(hit.locally_deleted).toBe(false);
  }
  expect(all.hits.find((h) => path.basename(h.path) === "계약.md")?.snippet).toContain("위약금");

  // 백엔드 처리: 검색 로그(건수·검색 범위·지연). 질문과 경로는 남기지 않는다.
  const done = await waitForBackendLog((r) => r.event === "search.completed", { since });
  expect(done.results).toBe(3);
  expect(done.vector_used).toBe(true);
  expect(done.filters).toEqual(["directory"]);
  expect(done.coverage).toEqual({ bm25_docs: 3, vector_docs: 3, pending_docs: 0, total_docs: 3 });
  expect(Object.keys(done.latency_ms as object)).toEqual(
    expect.arrayContaining(["embed", "coverage", "sql", "total"]),
  );
  expect(JSON.stringify(done)).not.toContain("위약금");
  expect(JSON.stringify(done)).not.toContain(stamp);

  // 2) 형식 필터
  since = backendLogMark();
  const txt = search(["위약금", "--dir", folder, "--ext", "txt"]);
  expect(names(txt)).toEqual(["메모.txt"]);
  expect(txt.coverage.total_docs).toBe(1);
  const txtLog = await waitForBackendLog((r) => r.event === "search.completed", { since });
  expect(txtLog.filters).toEqual(["directory", "exts"]);

  // 3) 수정일 필터에 맞는 문서가 없으면 검색하지 않고 빈 결과 + 범위 0
  since = backendLogMark();
  const none = search(["위약금", "--dir", folder, "--to", "2000-01-01"]);
  expect(none.hits).toEqual([]);
  expect(none.coverage.total_docs).toBe(0);
  const noneLog = await waitForBackendLog((r) => r.event === "search.completed", { since });
  expect(noneLog.results).toBe(0);
  expect(findBackendLogs((r) => r.event === "search.embed_failed", since)).toHaveLength(0);
});
