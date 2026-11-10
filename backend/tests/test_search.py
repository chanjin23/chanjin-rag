"""Hybrid 검색과 검색 범위 (PRD 5.2-2, F-07, TRD 7.1, 7.2).

실제 테스트 DB(ParadeDB: pgvector + pg_search)에 합성 문서·청크를 넣고 검색한다. BM25 토큰은
문서·질문 모두 같은 Kiwi 함수로 만들고, 벡터는 방향을 직접 정한 단위 벡터를 써서 순위를 예측한다.
`session` 픽스처가 테스트마다 롤백한다.
"""

import json
import math
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs
from typer.testing import CliRunner

from app.cli import app
from app.db.enums import DocumentStatus
from app.db.models import EMBED_DIM, Chunk, Document
from app.ingest.chunking import filename_chunk
from app.ingest.tokenize import bm25_text
from app.providers.errors import RateLimited
from app.providers.fake import FakeEmbeddingProvider
from app.query.search import (
    RRF_K,
    SearchFilters,
    SearchHit,
    compute_coverage,
    directory_prefix,
    hybrid_search,
    make_snippet,
    search,
)

SHA = "a" * 64
runner = CliRunner()
FAR = 2000  # 질문 벡터(0번 축)와 직교하는 축


def vec(*weights: tuple[int, float]) -> list[float]:
    """(축, 값) 목록으로 만든 단위 벡터."""
    v = [0.0] * EMBED_DIM
    for axis, w in weights:
        v[axis] = w
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


Q = vec((0, 1.0))  # 질문 벡터


async def add_doc(
    session: AsyncSession,
    path: str,
    chunks: Sequence[tuple[str, list[float] | None]] = (),
    *,
    status: DocumentStatus = DocumentStatus.READY,
    mtime: datetime = datetime(2026, 11, 1, tzinfo=UTC),
    locally_deleted: bool = False,
) -> tuple[Document, list[Chunk]]:
    """문서 하나와 청크(본문, 임베딩 또는 None)를 넣는다. 본문은 Kiwi로 BM25 토큰화."""
    filename = path.replace("/", "\\").rsplit("\\", 1)[-1]
    doc = Document(
        path=path,
        path_key=path,
        filename=filename,
        ext="." + filename.rsplit(".", 1)[-1],  # 수집 경로(intake)와 같이 점 포함
        size=100,
        mtime=mtime,
        sha256=SHA,
        status=status,
        locally_deleted=locally_deleted,
    )
    rows = [
        Chunk(
            seq=i,
            location={"page": i + 1},
            heading_path=[],
            filename=filename,
            content=content,
            content_sha256=SHA,
            bm25_text=bm25_text(content),
            token_count=len(content),
            embedding=embedding,
            embed_model="fake" if embedding is not None else None,
        )
        for i, (content, embedding) in enumerate(chunks)
    ]
    doc.chunks = rows
    session.add(doc)
    await session.flush()
    return doc, rows


def by_chunk(hits: Sequence[SearchHit]) -> dict[int, SearchHit]:
    return {h.chunk_id: h for h in hits}


def expected_rrf(hit: SearchHit) -> float:
    return sum(1.0 / (RRF_K + r) for r in (hit.vector_rank, hit.bm25_rank) if r is not None)


async def test_rrf_merges_vector_and_bm25(session: AsyncSession) -> None:
    _, (both,) = await add_doc(
        session, r"C:\docs\계약\a.md", [("위약금 조항은 계약 금액의 10%이다.", Q)]
    )
    _, (kw_strong,) = await add_doc(
        session, r"C:\docs\계약\b.md", [("위약금 위약금 위약금 규정", vec((FAR, 1.0)))]
    )
    _, (vec_only,) = await add_doc(
        session, r"C:\docs\메모\c.md", [("오늘 날씨는 맑음", vec((0, 0.9), (1, 0.1)))]
    )

    result = await hybrid_search(session, "위약금", query_vector=Q)
    hits = result.hits
    found = by_chunk(hits)

    # 둘 다에 있는 청크가 1위, 벡터에만 있는 청크도 들어온다
    assert hits[0].chunk_id == both.id
    assert found[both.id].vector_rank == 1 and found[both.id].bm25_rank is not None
    assert found[vec_only.id].vector_rank == 2 and found[vec_only.id].bm25_rank is None
    assert found[kw_strong.id].bm25_rank is not None
    # RRF(k=60) = Σ 1/(60 + 순위), 점수 내림차순, 최종 순위는 1부터
    for hit in hits:
        assert hit.rrf_score == pytest.approx(expected_rrf(hit))
    assert [h.rrf_score for h in hits] == sorted((h.rrf_score for h in hits), reverse=True)
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
    assert result.vector_used and result.keyword_tokens == 1

    # 결과 항목: 청크·문서 ID, 경로, 위치, 수정일, 스니펫, 로컬 삭제 여부
    top = found[both.id]
    assert top.document_id == both.document_id
    assert top.path == r"C:\docs\계약\a.md"
    assert top.location == {"page": 1}
    assert top.mtime == datetime(2026, 11, 1, tzinfo=UTC)
    assert "위약금" in top.snippet
    assert top.locally_deleted is False
    assert top.log_entry()["rrf"] == pytest.approx(top.rrf_score, abs=1e-6)

    # 지연 측정(질의 로그용)
    assert {"coverage", "sql", "total"} <= set(result.latency_ms)
    assert all(v >= 0 for v in result.latency_ms.values())

    # 같은 트랜잭션에서 HNSW 반복 탐색이 켜져 있다
    assert await session.scalar(text("SHOW hnsw.iterative_scan")) == "relaxed_order"

    # 각 목록은 상위 pool개만: pool=1이면 벡터 1위와 BM25 1위만 남는다
    small = await hybrid_search(session, "위약금", query_vector=Q, pool=1)
    assert all(h.vector_rank in (None, 1) and h.bm25_rank in (None, 1) for h in small.hits)
    assert 1 <= len(small.hits) <= 2

    # 벡터가 없으면 BM25로만
    kw_only = await hybrid_search(session, "위약금", query_vector=None)
    assert {h.chunk_id for h in kw_only.hits} == {both.id, kw_strong.id}
    assert all(h.vector_rank is None for h in kw_only.hits)
    assert not kw_only.vector_used


async def test_unembedded_chunks_found_by_bm25(session: AsyncSession) -> None:
    _, (embedded,) = await add_doc(session, r"C:\docs\a.md", [("배포 일정 안내", Q)])
    _, (pending, unrelated) = await add_doc(
        session,
        r"C:\docs\b.md",
        [("위약금 조항 정리", None), ("휴가 신청 방법", None)],
        status=DocumentStatus.INDEXED,
    )
    # 10MB 초과(meta_only) 문서는 파일명 청크로만 검색된다
    meta = filename_chunk("위약금_계약서.pdf", r"C:\docs\큰파일\위약금_계약서.pdf")
    meta_doc = Document(
        path=r"C:\docs\큰파일\위약금_계약서.pdf",
        path_key=r"C:\docs\큰파일\위약금_계약서.pdf",
        filename="위약금_계약서.pdf",
        ext=".pdf",
        size=20 * 1024 * 1024,
        mtime=datetime(2026, 10, 1, tzinfo=UTC),
        sha256=SHA,
        status=DocumentStatus.META_ONLY,
    )
    meta_doc.chunks = [
        Chunk(
            seq=0,
            location=meta.location,
            heading_path=meta.heading_path,
            filename=meta_doc.filename,
            content=meta.content,
            content_sha256=meta.content_sha256,
            bm25_text=meta.bm25_text,
            token_count=meta.token_count,
        )
    ]
    session.add(meta_doc)
    await session.flush()

    result = await hybrid_search(session, "위약금", query_vector=Q)
    found = by_chunk(result.hits)

    # 임베딩 없는 청크는 벡터 순위 없이 BM25로만 들어온다
    assert found[pending.id].vector_rank is None
    assert found[pending.id].bm25_rank is not None
    assert found[pending.id].rrf_score == pytest.approx(1.0 / (RRF_K + found[pending.id].bm25_rank))
    meta_hit = next(h for h in result.hits if h.document_id == meta_doc.id)
    assert meta_hit.vector_rank is None and meta_hit.bm25_rank is not None
    # 임베딩 없는 청크가 키워드도 안 맞으면 찾을 길이 없다
    assert unrelated.id not in found
    # 임베딩된 청크는 키워드가 안 맞아도 벡터로 들어온다
    assert found[embedded.id].vector_rank == 1 and found[embedded.id].bm25_rank is None


async def test_directory_ext_mtime_filters(session: AsyncSession) -> None:
    def day(d: int) -> datetime:
        return datetime(2026, 10, d, tzinfo=UTC)

    specs: dict[str, tuple[str, str, list[float] | None, int]] = {
        "a_md": (r"C:\Docs\A\note.md", "예산 계획", Q, 1),
        "a_pdf": (r"C:\Docs\A\sub\report.PDF", "예산 보고", Q, 10),
        "ab_md": (r"C:\Docs\AB\x.md", "예산 메모", Q, 20),
        "b_txt": (r"C:\Docs\B\y.txt", "예산 초안", None, 30),  # 임베딩 전(BM25만)
    }
    ids: dict[str, int] = {}
    for name, (path, body, emb, d) in specs.items():
        doc, _ = await add_doc(session, path, [(body, emb)], mtime=day(d))
        ids[name] = doc.id

    async def doc_names(filters: SearchFilters) -> set[str]:
        result = await hybrid_search(session, "예산", query_vector=Q, filters=filters)
        found = {h.document_id for h in result.hits}
        return {name for name, i in ids.items() if i in found}

    assert await doc_names(SearchFilters()) == set(ids)

    # 디렉터리: 접두어(하위 폴더 포함), 대소문자·슬래시·끝 구분자 무시, A가 AB에 걸리지 않음
    assert await doc_names(SearchFilters(directory=r"C:\Docs\A")) == {"a_md", "a_pdf"}
    assert await doc_names(SearchFilters(directory="c:/docs/a/")) == {"a_md", "a_pdf"}
    assert await doc_names(SearchFilters(directory=r"C:\Docs\A\sub")) == {"a_pdf"}
    assert await doc_names(SearchFilters(directory=r"C:\없는폴더")) == set()
    assert directory_prefix(r"C:\Docs\A\\") == "c:\\docs\\a\\"
    assert directory_prefix("  ") is None

    # 형식: 소문자, 점 무시
    assert await doc_names(SearchFilters(exts=[".PDF"])) == {"a_pdf"}
    assert await doc_names(SearchFilters(exts=["md", "txt"])) == {"a_md", "ab_md", "b_txt"}

    # 수정일 범위(양 끝 포함)
    assert await doc_names(SearchFilters(mtime_from=day(10), mtime_to=day(20))) == {
        "a_pdf",
        "ab_md",
    }
    assert await doc_names(SearchFilters(mtime_from=day(21))) == {"b_txt"}
    assert await doc_names(SearchFilters(mtime_to=day(1))) == {"a_md"}

    # 조합
    combined = SearchFilters(directory=r"C:\Docs", exts=["md"], mtime_from=day(5))
    assert await doc_names(combined) == {"ab_md"}
    assert combined.applied() == {
        "directory": r"C:\Docs",
        "exts": ["md"],
        "mtime_from": day(5).isoformat(),
    }


async def test_include_deleted_toggle(session: AsyncSession) -> None:
    live, _ = await add_doc(session, r"C:\docs\live.md", [("견적서 검토", Q)])
    near = vec((0, 0.8), (1, 0.2))
    gone, _ = await add_doc(
        session, r"C:\docs\gone.md", [("견적서 초안", near)], locally_deleted=True
    )

    # 기본: 로컬 삭제 문서도 검색되고 표시만 한다
    default = await hybrid_search(session, "견적서", query_vector=Q)
    flags = {h.document_id: h.locally_deleted for h in default.hits}
    assert flags == {live.id: False, gone.id: True}
    assert default.coverage.total_docs == 2
    assert default.coverage.filters_applied == {}

    # include_deleted=false면 빠진다(검색 범위에서도)
    excluded = await hybrid_search(
        session, "견적서", query_vector=Q, filters=SearchFilters(include_deleted=False)
    )
    assert {h.document_id for h in excluded.hits} == {live.id}
    assert excluded.coverage.total_docs == 1
    assert excluded.coverage.bm25_docs == 1
    assert excluded.coverage.filters_applied == {"include_deleted": False}


async def test_max_three_chunks_per_document(session: AsyncSession) -> None:
    # 문서 A: 질문과 가까운 청크 6개(가까운 순서대로), 문서 B: 2개
    big, big_chunks = await add_doc(
        session,
        r"C:\docs\big.md",
        [(f"회의록 안건 {i}", vec((0, 1.0), (1 + i, 0.05 * (i + 1)))) for i in range(6)],
    )
    small, _ = await add_doc(
        session,
        r"C:\docs\small.md",
        [(f"회의록 결론 {i}", vec((0, 0.5), (10 + i, 1.0))) for i in range(2)],
    )

    result = await hybrid_search(session, "회의록", query_vector=Q)
    per_doc = Counter(h.document_id for h in result.hits)
    assert per_doc == {big.id: 3, small.id: 2}
    # 문서 A에서는 RRF 점수가 높은 3개가 남는다(벡터 1~3위)
    kept = [h for h in result.hits if h.document_id == big.id]
    assert [h.vector_rank for h in kept] == [1, 2, 3]
    assert {h.chunk_id for h in kept} == {c.id for c in big_chunks[:3]}
    # 상한을 바꿀 수 있다
    one = await hybrid_search(session, "회의록", query_vector=Q, per_document=1)
    assert Counter(h.document_id for h in one.hits) == {big.id: 1, small.id: 1}


async def test_coverage_counts_with_filters(session: AsyncSession) -> None:
    # 의미 검색 가능(임베딩된 청크 있음)
    await add_doc(session, r"C:\work\a.md", [("가", Q)], status=DocumentStatus.READY)
    # 임베딩 중: 일부만 임베딩됨 → 의미 검색 가능 + 처리 중
    await add_doc(
        session,
        r"C:\work\b.pdf",
        [("나", Q), ("다", None)],
        status=DocumentStatus.EMBEDDING,
    )
    # BM25만
    await add_doc(session, r"C:\work\c.md", [("라", None)], status=DocumentStatus.INDEXED)
    # 파일명만(meta_only): 파일명 청크 → BM25 가능
    await add_doc(session, r"C:\other\d.pdf", [("d.pdf", None)], status=DocumentStatus.META_ONLY)
    # 대기·처리 중(청크 없음)
    await add_doc(session, r"C:\work\e.md", status=DocumentStatus.QUEUED)
    await add_doc(session, r"C:\other\f.md", status=DocumentStatus.EXTRACTING)
    # 실패(청크 없음): 어느 쪽도 아님
    await add_doc(session, r"C:\work\g.docx", status=DocumentStatus.FAILED)
    # 로컬 삭제됨
    await add_doc(session, r"C:\work\h.md", [("마", Q)], locally_deleted=True)

    everything = await compute_coverage(session)
    assert everything.as_dict() == {
        "bm25_docs": 5,
        "vector_docs": 3,
        "pending_docs": 3,
        "total_docs": 8,
        "filters_applied": {},
    }

    work = await compute_coverage(session, SearchFilters(directory=r"C:\work"))
    assert (work.bm25_docs, work.vector_docs, work.pending_docs, work.total_docs) == (4, 3, 2, 6)
    assert work.filters_applied == {"directory": r"C:\work"}

    md_live = SearchFilters(directory=r"C:\work", exts=["md"], include_deleted=False)
    cov = await compute_coverage(session, md_live)
    assert (cov.bm25_docs, cov.vector_docs, cov.pending_docs, cov.total_docs) == (2, 1, 1, 3)
    assert cov.filters_applied == {
        "directory": r"C:\work",
        "exts": ["md"],
        "include_deleted": False,
    }

    since = SearchFilters(mtime_from=datetime(2026, 11, 2, tzinfo=UTC))
    empty = await hybrid_search(session, "가", query_vector=Q, filters=since)
    # 필터에 맞는 문서가 0개면 검색하지 않는다
    assert empty.coverage.total_docs == 0
    assert empty.hits == []
    assert empty.coverage.filters_applied == {"mtime_from": "2026-11-02T00:00:00+00:00"}


# ---------------------------------------------------------------------------
# 보조: 질문 임베딩 + 로그, 임베딩 실패 시 BM25로만, 스니펫
# ---------------------------------------------------------------------------


async def test_search_embeds_query_and_logs(session: AsyncSession) -> None:
    embedder = FakeEmbeddingProvider(dim=EMBED_DIM)
    text_a = "분기 매출 보고서 요약"
    passage = (await embedder.embed([text_a], input_type="passage"))[0]
    doc, _ = await add_doc(session, r"C:\docs\sales.md", [(text_a, passage)])

    with capture_logs() as logs:
        result = await search(session, embedder, "분기 매출 보고서 요약")
    assert result.hits[0].document_id == doc.id
    assert result.hits[0].vector_rank == 1 and result.hits[0].bm25_rank == 1
    assert result.latency_ms["embed"] >= 0
    assert result.latency_ms["total"] >= result.latency_ms["sql"]

    done = [e for e in logs if e["event"] == "search.completed"]
    assert len(done) == 1
    entry: dict[str, Any] = done[0]
    assert entry["results"] == 1 and entry["vector_used"] is True
    assert entry["coverage"]["vector_docs"] == 1
    assert "embed" in entry["latency_ms"]
    # 질문·경로는 로그에 남기지 않는다
    assert "매출" not in str(entry) and "sales" not in str(entry)


async def test_search_falls_back_to_bm25_when_embedding_fails(session: AsyncSession) -> None:
    class FailingEmbedder(FakeEmbeddingProvider):
        async def embed(self, texts: Sequence[str], *, input_type: Any) -> list[list[float]]:
            raise RateLimited("한도 초과")

    _, (chunk,) = await add_doc(session, r"C:\docs\a.md", [("백업 절차 안내", Q)])
    with capture_logs() as logs:
        result = await search(session, FailingEmbedder(dim=EMBED_DIM), "백업 절차")
    assert not result.vector_used
    assert [h.chunk_id for h in result.hits] == [chunk.id]
    assert result.hits[0].vector_rank is None
    assert any(e["event"] == "search.embed_failed" for e in logs)


def test_make_snippet_centers_on_query_token() -> None:
    body = "앞부분 " * 100 + "핵심 위약금 문장 " + "뒷부분 " * 100
    snippet = make_snippet(body, ["위약금"], size=60)
    assert "위약금" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert make_snippet("짧은\n\n본문", ["x"]) == "짧은 본문"


# ---------------------------------------------------------------------------
# 보조: 실제 수집 경로(ingest-local: 파싱 → 청킹 → Kiwi → Fake 임베딩)로 넣은 문서를 CLI로 검색
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_db(migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """CLI는 자기 연결로 commit하므로 앞뒤로 수집 테이블을 비운다(test_ingest_local과 같은 방식)."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("SYNC_ALLOWED_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("UPLOAD_TMP_DIR", str(tmp_path / "uploads"))
    saved_logging = structlog.get_config()
    scheme, sep, rest = migrated_db.partition("://")
    with psycopg.connect(f"{scheme.split('+', 1)[0]}{sep}{rest}", autocommit=True) as conn:

        def clean() -> None:
            conn.execute("DELETE FROM documents")
            conn.execute("DELETE FROM embed_quota")

        clean()
        try:
            yield
        finally:
            clean()
            structlog.configure(**saved_logging)


def test_search_cli_over_ingested_folder(cli_db: None, tmp_path: Path) -> None:
    folder = tmp_path / "root" / "notes"
    (folder / "sub").mkdir(parents=True)
    (folder / "계약.md").write_text(
        "# 계약 조건\n\n위약금은 계약 금액의 10%로 한다.\n", encoding="utf-8"
    )
    memo = "위약금 관련 메모: 다음 주에 확인.\n"
    (folder / "sub" / "메모.txt").write_text(memo, encoding="utf-8")
    (folder / "일정.md").write_text("# 일정\n\n배포는 금요일에 한다.\n", encoding="utf-8")
    res = runner.invoke(app, ["ingest-local", str(folder)])
    assert res.exit_code == 0, res.output

    res = runner.invoke(app, ["search", "위약금 조건", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output.strip().splitlines()[-1])
    assert payload["vector_used"] is True
    assert payload["coverage"] == {
        "bm25_docs": 3,
        "vector_docs": 3,
        "pending_docs": 0,
        "total_docs": 3,
        "filters_applied": {},
    }
    names = [Path(h["path"]).name for h in payload["hits"]]
    # 키워드가 맞는 두 문서가 위(벡터·BM25 1·2위가 엇갈려 RRF 동점일 수 있음), 일정은 벡터로만
    assert set(names[:2]) == {"계약.md", "메모.txt"} and names[2] == "일정.md"
    keyword_hits = {Path(h["path"]).name for h in payload["hits"] if h["bm25_rank"] is not None}
    assert keyword_hits == {"계약.md", "메모.txt"}

    res = runner.invoke(app, ["search", "위약금", "--json", "--dir", str(folder / "sub")])
    payload = json.loads(res.output.strip().splitlines()[-1])
    assert {Path(h["path"]).name for h in payload["hits"]} == {"메모.txt"}
    assert payload["coverage"]["total_docs"] == 1

    # 수집 경로는 ext를 점 포함(`.txt`)으로 저장한다. 필터는 점 유무와 상관없이 맞는다.
    for ext in ("txt", ".TXT"):
        res = runner.invoke(app, ["search", "위약금", "--json", "--ext", ext])
        payload = json.loads(res.output.strip().splitlines()[-1])
        assert [Path(h["path"]).name for h in payload["hits"]] == ["메모.txt"]

    res = runner.invoke(app, ["search", "위약금", "--json", "--ext", "md", "--to", "2000-01-01"])
    payload = json.loads(res.output.strip().splitlines()[-1])
    assert payload["hits"] == [] and payload["coverage"]["total_docs"] == 0

    res = runner.invoke(app, ["search", "위약금", "--from", "2000-01-01"])
    assert res.exit_code == 0, res.output
    assert "검색 범위: 키워드 3 · 의미 3 · 대기 0" in res.output
    assert "계약.md" in res.output
