"""Hybrid 검색과 검색 범위(coverage) (PRD 5.2-2, F-07, TRD 7.1, 7.2).

벡터(pgvector HNSW) 상위 50개와 BM25(pg_search) 상위 50개를 SQL 하나 안에서 RRF(k=60)로
합쳐 후보 30개를 고른다. 같은 문서의 청크가 후보를 독점하지 않도록 문서당 최대 3개로 자른다.

- BM25: 질문을 문서와 같은 Kiwi 함수(`app.ingest.tokenize.bm25_text`)로 토큰화하고, pg_search
  0.26.5의 `|||`(match disjunction: 필드 토크나이저로 자른 뒤 OR)로 `bm25_text`·`filename`·
  `heading_path` 세 필드를 찾는다. `@@@ '문자열'`은 쿼리 파서를 거쳐 `:`·괄호·따옴표가 섞인
  토큰에서 깨질 수 있어 쓰지 않는다. 점수는 `pdb.score(id)`.
- 벡터: `embedding <=> 질문 벡터`(코사인 거리). 임베딩이 없는 청크는 벡터 후보에 없고 BM25로만
  들어온다. 필터와 함께 쓸 때 HNSW 결과가 모자라지 않게 `hnsw.iterative_scan`을 켠다.
- 순위: 각 후보 목록은 안쪽 질의에서 `ORDER BY … LIMIT 50`으로 먼저 자르고, 바깥에서
  `ROW_NUMBER()`로 순위를 매긴다. 창 함수를 LIMIT과 같은 단계에 두면 필터에 걸린 모든 행의 거리를
  계산·정렬하게 되어 색인을 못 쓰고, `relaxed_order`로 조금 어긋난 순서도 바깥에서 바로잡는다.
- 필터: 디렉터리(경로 접두어), 형식(점 없이 비교), 수정일 범위, 로컬 삭제 포함 여부(기본 포함).
"""

import re
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import DocumentStatus
from app.db.keys import make_path_key
from app.ingest.tokenize import bm25_tokens
from app.providers.base import EmbeddingProvider
from app.providers.errors import ProviderError

log = get_logger(__name__)

# TRD 7.2
CANDIDATE_POOL = 50  # 벡터·BM25 각각의 상위 개수
RESULT_LIMIT = 30  # RRF로 합친 뒤 후보 수
RRF_K = 60
MAX_PER_DOCUMENT = 3
# HNSW 탐색 폭. 기본값(40)이 후보 수(50)보다 작아 넉넉히 둔다.
HNSW_EF_SEARCH = 100
SNIPPET_CHARS = 200

# 검색 범위의 "대기, 처리 중"(TRD 7.1, 5.1 화면 표기: 대기=queued, 처리 중=extracting·embedding)
PENDING_STATUSES = (
    DocumentStatus.QUEUED.value,
    DocumentStatus.EXTRACTING.value,
    DocumentStatus.EMBEDDING.value,
)


class SearchFilters(BaseModel):
    """검색 필터(TRD 7.2). 질의 로그의 `filters`에도 그대로 저장한다."""

    directory: str | None = Field(default=None, description="경로 접두어(폴더). 하위 폴더 포함")
    exts: list[str] | None = Field(default=None, description="형식(확장자, 점 없이)")
    mtime_from: datetime | None = Field(default=None, description="수정일 시작(포함)")
    mtime_to: datetime | None = Field(default=None, description="수정일 끝(포함)")
    include_deleted: bool = Field(default=True, description="로컬 삭제 문서 포함 여부")

    def applied(self) -> dict[str, Any]:
        """기본값이 아닌 필터만(coverage의 `filters_applied`, 화면 안내용)."""
        out: dict[str, Any] = {}
        prefix = directory_prefix(self.directory)
        if prefix is not None:
            out["directory"] = self.directory
        exts = normalize_exts(self.exts)
        if exts:
            out["exts"] = exts
        if self.mtime_from is not None:
            out["mtime_from"] = self.mtime_from.isoformat()
        if self.mtime_to is not None:
            out["mtime_to"] = self.mtime_to.isoformat()
        if not self.include_deleted:
            out["include_deleted"] = False
        return out


def directory_prefix(directory: str | None) -> str | None:
    """디렉터리 필터 → `path_key` 접두어(NFC 소문자, `\\` 구분, 끝에 `\\`).

    끝에 구분자를 붙여 `C:\\a`가 `C:\\ab\\x.md`에 걸리지 않게 한다. `LIKE`는 `\\`를 이스케이프
    문자로 해석해 Windows 경로에서 틀리므로 SQL에서는 `starts_with`로 비교한다.
    `/`로 시작하는 POSIX 경로(Linux에서 `ingest-local`로 넣은 문서)는 `/` 구분을 유지한다.
    """
    if directory is None:
        return None
    raw = make_path_key(directory.strip())
    sep = "/" if raw.startswith("/") else "\\"
    key = raw.replace("\\" if sep == "/" else "/", sep).rstrip(sep)
    if not key:
        return None
    return key + sep


def normalize_exts(exts: Sequence[str] | None) -> list[str]:
    if not exts:
        return []
    out: list[str] = []
    for ext in exts:
        e = ext.strip().lower().lstrip(".")
        if e and e not in out:
            out.append(e)
    return out


def _filter_sql(filters: SearchFilters) -> tuple[str, dict[str, Any]]:
    """documents(별칭 d)에 거는 필터 조건(`AND …`)과 바인드 값."""
    parts: list[str] = []
    params: dict[str, Any] = {}
    prefix = directory_prefix(filters.directory)
    if prefix is not None:
        parts.append("starts_with(d.path_key, :f_dir)")
        params["f_dir"] = prefix
    exts = normalize_exts(filters.exts)
    if exts:
        # documents.ext는 수집 경로에서 점을 포함해 저장된다(`.md`). 점 유무와 상관없이 비교한다.
        parts.append("ltrim(d.ext, '.') = ANY(:f_exts)")
        params["f_exts"] = exts
    if filters.mtime_from is not None:
        parts.append("d.mtime >= :f_mtime_from")
        params["f_mtime_from"] = filters.mtime_from
    if filters.mtime_to is not None:
        parts.append("d.mtime <= :f_mtime_to")
        params["f_mtime_to"] = filters.mtime_to
    if not filters.include_deleted:
        parts.append("NOT d.locally_deleted")
    sql = "".join(f" AND {p}" for p in parts)
    return sql, params


@dataclass(frozen=True)
class Coverage:
    """질의 시점에 필터를 적용한 검색 범위(TRD 7.1).

    - `bm25_docs`: 키워드 검색 가능(청크가 1개 이상, 파일명 청크 포함)
    - `vector_docs`: 의미 검색 가능(임베딩된 청크가 1개 이상)
    - `pending_docs`: 대기·처리 중(`queued`, `extracting`, `embedding`)
    - `total_docs`: 필터에 맞는 전체 문서 수(0이면 검색하지 않음)
    """

    bm25_docs: int
    vector_docs: int
    pending_docs: int
    total_docs: int
    filters_applied: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchHit:
    """검색 결과 한 건. 순위는 1부터, 해당 목록에 없으면 None."""

    chunk_id: int
    document_id: int
    path: str
    filename: str
    ext: str
    location: dict[str, Any]
    heading_path: list[str]
    mtime: datetime
    snippet: str
    content: str
    locally_deleted: bool
    rrf_score: float
    vector_rank: int | None
    bm25_rank: int | None
    rank: int

    def log_entry(self) -> dict[str, Any]:
        """질의 로그 `retrieved`용(경로·본문 없이 ID와 점수만)."""
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "rank": self.rank,
            "rrf": round(self.rrf_score, 6),
            "vector_rank": self.vector_rank,
            "bm25_rank": self.bm25_rank,
        }


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    coverage: Coverage
    filters: SearchFilters
    # 단계별 지연(ms): embed(질문 임베딩, `search`에서만), coverage, sql, total
    latency_ms: dict[str, float]
    vector_used: bool
    keyword_tokens: int


def _vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def make_snippet(content: str, tokens: Sequence[str], size: int = SNIPPET_CHARS) -> str:
    """본문에서 질문 토큰이 처음 나오는 곳 주변을 `size`자로 자른다(공백은 하나로)."""
    flat = re.sub(r"\s+", " ", content).strip()
    if len(flat) <= size:
        return flat
    lower = flat.lower()
    positions = [p for t in tokens if len(t) >= 2 and (p := lower.find(t.lower())) >= 0]
    start = 0
    if positions:
        start = max(0, min(positions) - size // 4)
    start = min(start, len(flat) - size)
    snippet = flat[start : start + size]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + size < len(flat) else ""
    return f"{prefix}{snippet}{suffix}"


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


async def compute_coverage(session: AsyncSession, filters: SearchFilters | None = None) -> Coverage:
    """필터를 적용한 검색 범위 문서 수(TRD 7.1)."""
    filters = filters or SearchFilters()
    where, params = _filter_sql(filters)
    row = (
        await session.execute(
            text(
                f"""
                SELECT count(*) AS total_docs,
                       count(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM chunks c WHERE c.document_id = d.id
                       )) AS bm25_docs,
                       count(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM chunks c
                           WHERE c.document_id = d.id AND c.embedding IS NOT NULL
                       )) AS vector_docs,
                       count(*) FILTER (WHERE d.status = ANY(:pending)) AS pending_docs
                FROM documents d
                WHERE true{where}
                """
            ),
            {**params, "pending": list(PENDING_STATUSES)},
        )
    ).one()
    return Coverage(
        bm25_docs=int(row.bm25_docs),
        vector_docs=int(row.vector_docs),
        pending_docs=int(row.pending_docs),
        total_docs=int(row.total_docs),
        filters_applied=filters.applied(),
    )


def _search_sql(where: str, *, use_vector: bool, use_bm25: bool) -> str:
    empty = "SELECT NULL::bigint AS id, NULL::bigint AS r WHERE false"
    vec = (
        f"""
        SELECT id, ROW_NUMBER() OVER (ORDER BY dist, id) AS r
        FROM (
            SELECT c.id, c.embedding <=> CAST(:qvec AS halfvec) AS dist
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL{where}
            ORDER BY c.embedding <=> CAST(:qvec AS halfvec)
            LIMIT :pool
        ) v
        """
        if use_vector
        else empty
    )
    kw = (
        f"""
        SELECT id, ROW_NUMBER() OVER (ORDER BY score DESC, id) AS r
        FROM (
            SELECT c.id, pdb.score(c.id) AS score
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE (c.bm25_text ||| :qtokens
                   OR c.filename ||| :qtokens
                   OR c.heading_path ||| :qtokens){where}
            ORDER BY pdb.score(c.id) DESC
            LIMIT :pool
        ) k
        """
        if use_bm25
        else empty
    )
    return f"""
        WITH vec AS ({vec}),
        kw AS ({kw}),
        fused AS (
            SELECT id, vec.r AS vector_rank, kw.r AS bm25_rank,
                   COALESCE(1.0 / (:rrf_k + vec.r), 0)
                   + COALESCE(1.0 / (:rrf_k + kw.r), 0) AS rrf
            FROM vec FULL OUTER JOIN kw USING (id)
        ),
        capped AS (
            SELECT f.*, c.document_id,
                   ROW_NUMBER() OVER (PARTITION BY c.document_id ORDER BY f.rrf DESC, f.id)
                       AS doc_rank
            FROM fused f JOIN chunks c ON c.id = f.id
        )
        SELECT x.id AS chunk_id, x.document_id, x.vector_rank, x.bm25_rank, x.rrf,
               d.path, d.filename, d.ext, d.mtime, d.locally_deleted,
               c.location, c.heading_path, c.content
        FROM capped x
        JOIN chunks c ON c.id = x.id
        JOIN documents d ON d.id = x.document_id
        WHERE x.doc_rank <= :per_doc
        ORDER BY x.rrf DESC, x.id
        LIMIT :result_limit
    """


async def hybrid_search(
    session: AsyncSession,
    query: str,
    *,
    query_vector: Sequence[float] | None,
    filters: SearchFilters | None = None,
    keyword_query: str | None = None,
    limit: int = RESULT_LIMIT,
    pool: int = CANDIDATE_POOL,
    per_document: int = MAX_PER_DOCUMENT,
    rrf_k: int = RRF_K,
    embed_ms: float | None = None,
) -> SearchResult:
    """Hybrid 검색(TRD 7.2). `query_vector`가 없으면 BM25로만 찾는다.

    `embed_ms`: 호출한 쪽이 질문 임베딩에 쓴 시간. 주면 `latency_ms`의 `embed`와 `total`에 넣는다.

    BM25 검색어는 `query`(와 있으면 `keyword_query`)의 Kiwi 토큰. 필터에 맞는 문서가 0개면
    SQL을 실행하지 않는다(TRD 7.1). 세션의 트랜잭션 안에서 `SET LOCAL`로 HNSW 설정을 바꾼다.
    """
    filters = filters or SearchFilters()
    started = time.perf_counter()

    tokens: list[str] = []
    for t in bm25_tokens(query) + (bm25_tokens(keyword_query) if keyword_query else []):
        if t not in tokens:
            tokens.append(t)
    qtokens = " ".join(tokens)

    latency: dict[str, float] = {}
    if embed_ms is not None:
        latency["embed"] = embed_ms
    t0 = time.perf_counter()
    coverage = await compute_coverage(session, filters)
    latency["coverage"] = _ms(t0)

    use_vector = query_vector is not None and len(query_vector) > 0
    use_bm25 = bool(qtokens)
    hits: list[SearchHit] = []
    t0 = time.perf_counter()
    if coverage.total_docs > 0 and (use_vector or use_bm25):
        where, params = _filter_sql(filters)
        if use_vector:
            await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(HNSW_EF_SEARCH)}"))
            params["qvec"] = _vector_literal(query_vector or [])
        if use_bm25:
            params["qtokens"] = qtokens
        # pg_search가 documents 조인에서 JoinScan을 못 쓴다는 경고를 매번 내지 않게 한다
        await session.execute(text("SET LOCAL paradedb.planner_warnings = 'Off'"))
        rows = (
            await session.execute(
                text(_search_sql(where, use_vector=use_vector, use_bm25=use_bm25)),
                {
                    **params,
                    "pool": pool,
                    "rrf_k": rrf_k,
                    "per_doc": per_document,
                    "result_limit": limit,
                },
            )
        ).all()
        hits = [
            SearchHit(
                chunk_id=int(r.chunk_id),
                document_id=int(r.document_id),
                path=r.path,
                filename=r.filename,
                ext=r.ext,
                location=dict(r.location or {}),
                heading_path=list(r.heading_path or []),
                mtime=r.mtime,
                snippet=make_snippet(r.content, tokens),
                content=r.content,
                locally_deleted=bool(r.locally_deleted),
                rrf_score=float(r.rrf),
                vector_rank=int(r.vector_rank) if r.vector_rank is not None else None,
                bm25_rank=int(r.bm25_rank) if r.bm25_rank is not None else None,
                rank=i,
            )
            for i, r in enumerate(rows, start=1)
        ]
    latency["sql"] = _ms(t0)
    latency["total"] = round(_ms(started) + (embed_ms or 0.0), 2)

    result = SearchResult(
        hits=hits,
        coverage=coverage,
        filters=filters,
        latency_ms=latency,
        vector_used=use_vector,
        keyword_tokens=len(tokens),
    )
    log.info(
        "search.completed",
        results=len(hits),
        documents=len({h.document_id for h in hits}),
        vector_hits=sum(1 for h in hits if h.vector_rank is not None),
        bm25_hits=sum(1 for h in hits if h.bm25_rank is not None),
        vector_used=use_vector,
        keyword_tokens=len(tokens),
        filters=sorted(coverage.filters_applied),
        coverage={k: v for k, v in coverage.as_dict().items() if k != "filters_applied"},
        latency_ms=latency,
    )
    return result


async def search(
    session: AsyncSession,
    embedder: EmbeddingProvider | None,
    query: str,
    *,
    filters: SearchFilters | None = None,
    keyword_query: str | None = None,
    limit: int = RESULT_LIMIT,
) -> SearchResult:
    """질문을 임베딩(`input_type="query"`)한 뒤 `hybrid_search`를 실행한다.

    임베딩 호출이 실패하면(한도 초과·장애) 질의를 막지 않고 BM25로만 찾는다
    (`search.embed_failed` 로그, 결과의 `vector_used=False`).
    """
    vector: list[float] | None = None
    embed_ms = 0.0
    if embedder is not None and query.strip():
        t0 = time.perf_counter()
        try:
            vector = (await embedder.embed([query], input_type="query"))[0]
        except ProviderError as e:
            log.warning("search.embed_failed", error=type(e).__name__)
        embed_ms = _ms(t0)
    return await hybrid_search(
        session,
        query,
        query_vector=vector,
        filters=filters,
        keyword_query=keyword_query,
        limit=limit,
        embed_ms=embed_ms,
    )
