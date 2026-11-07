"""DB 스키마 (TRD 5장): 경로 키, 색인, 초기값, 상태 값 제약.

모든 데이터는 합성 값이다. `session` 픽스처가 테스트마다 롤백한다.
"""

import unicodedata
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.enums import DocumentStatus, JobStatus, JobType, SectionKind
from app.db.keys import make_path_key
from app.db.models import Chunk, Document, Job, Section, SyncConfig

SHA_A = "a" * 64
SHA_B = "b" * 64


def make_document(path: str, sha256: str = SHA_A) -> Document:
    filename = path.replace("/", "\\").rsplit("\\", 1)[-1]
    return Document(
        path=path,
        path_key=path,  # 모델이 NFC + 소문자로 정규화한다
        filename=filename,
        ext=filename.rsplit(".", 1)[-1],
        size=123,
        mtime=datetime(2026, 11, 1, tzinfo=UTC),
        sha256=sha256,
    )


def unit_vector(dim: int, hot: int) -> list[float]:
    v = [0.0] * dim
    v[hot] = 1.0
    return v


async def test_path_key_unique(session: AsyncSession) -> None:
    # 한글 파일명은 NFC/NFD 두 형태로 올 수 있고, Windows 경로는 대소문자를 구분하지 않는다
    nfc = r"C:\Chanjin\dev\메모\회의록 Report.MD"
    nfd = unicodedata.normalize("NFD", nfc).upper()
    assert nfc != nfd
    assert make_path_key(nfc) == make_path_key(nfd) == r"c:\chanjin\dev\메모\회의록 report.md"

    doc = make_document(nfc)
    session.add(doc)
    await session.flush()
    assert doc.path == nfc  # 원래 경로는 그대로 보관
    assert doc.path_key == make_path_key(nfc)
    assert doc.ext == "md"
    assert doc.status == DocumentStatus.QUEUED  # 서버 기본값

    # ORM으로 넣어도(정규화 후 같은 키) 막힌다
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(make_document(nfd, sha256=SHA_B))
            await session.flush()

    # DB 제약 자체도 unique다
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text(
                    "insert into documents (path, path_key, filename, ext, size, mtime, sha256) "
                    "values (:p, :k, 'x.md', 'md', 1, now(), :s)"
                ),
                {"p": nfd, "k": make_path_key(nfd), "s": SHA_B},
            )

    # 다른 경로는 들어간다
    session.add(make_document(r"C:\Chanjin\dev\메모\다른 파일.md"))
    await session.flush()
    count = await session.scalar(text("select count(*) from documents"))
    assert count == 2


async def test_vector_and_bm25_indexes_exist(session: AsyncSession) -> None:
    dim = get_settings().embed_dim
    rows = (
        await session.execute(
            text("select indexname, indexdef from pg_indexes where schemaname = 'public'")
        )
    ).all()
    indexes = {name: definition for name, definition in rows}

    # 벡터: HNSW(halfvec_cosine_ops, m=16, ef_construction=64), 컬럼은 halfvec(EMBED_DIM)
    hnsw = indexes["ix_chunks_embedding_hnsw"]
    assert "USING hnsw" in hnsw
    assert "halfvec_cosine_ops" in hnsw
    assert "m='16'" in hnsw and "ef_construction='64'" in hnsw
    col_type = await session.scalar(
        text(
            "select format_type(atttypid, atttypmod) from pg_attribute "
            "where attrelid = 'chunks'::regclass and attname = 'embedding'"
        )
    )
    assert col_type == f"halfvec({dim})"

    # BM25: pg_search, bm25_text(공백 토크나이저) + 파일명 + heading_path
    bm25 = indexes["ix_chunks_bm25"]
    assert "USING bm25" in bm25
    assert "bm25_text" in bm25 and "pdb.whitespace" in bm25
    assert "filename" in bm25
    assert "heading_path" in bm25

    # documents, jobs 색인
    assert "UNIQUE" in indexes["uq_documents_path_key"]
    assert "(sha256)" in indexes["ix_documents_sha256"]
    assert "(status)" in indexes["ix_documents_status"]
    assert "(locally_deleted)" in indexes["ix_documents_locally_deleted"]
    assert "(status, type, priority DESC)" in indexes["ix_jobs_queue"]

    # 실제로 검색되는지: 합성 문서 1개, 청크 2개
    doc = make_document(r"C:\Chanjin\dev\계약\위약금_계약서_2026.docx")
    doc.chunks = [
        Chunk(
            seq=0,
            location={"page": 1},
            heading_path=["1장 총칙", "위약금 조항"],
            filename=doc.filename,
            content="위약금은 계약 금액의 10%로 한다.",
            content_sha256=SHA_A,
            bm25_text="위약금 계약 금액 10",
            token_count=12,
            embedding=unit_vector(dim, 0),
        ),
        Chunk(
            seq=1,
            location={"page": 2},
            heading_path=["2장 기간"],
            filename=doc.filename,
            content="계약 기간은 1년이다.",
            content_sha256=SHA_B,
            bm25_text="계약 기간 1 년",
            token_count=8,
            embedding=None,  # 아직 임베딩 전(BM25로만 검색)
        ),
    ]
    session.add(doc)
    await session.flush()

    async def bm25_seqs(where: str, q: str) -> list[int]:
        result = await session.execute(
            text(f"select seq from chunks where {where} @@@ :q order by seq"), {"q": q}
        )
        return list(result.scalars())

    assert await bm25_seqs("bm25_text", "위약금") == [0]
    assert await bm25_seqs("bm25_text", "계약") == [0, 1]
    assert await bm25_seqs("filename", "계약서") == [0, 1]  # 파일명은 _ . 에서 잘린다
    assert await bm25_seqs("heading_path", "기간") == [1]

    nearest = await session.scalar(
        select(Chunk.seq)
        .where(Chunk.embedding.is_not(None))
        .order_by(Chunk.embedding.cosine_distance(unit_vector(dim, 0)))
        .limit(1)
    )
    assert nearest == 0

    # 문서를 지우면 청크도 지워진다(ON DELETE CASCADE)
    await session.execute(delete(Document).where(Document.id == doc.id))
    assert await session.scalar(text("select count(*) from chunks")) == 0


async def test_sync_config_seeded(session: AsyncSession) -> None:
    rows = (await session.scalars(select(SyncConfig))).all()
    assert len(rows) == 1
    cfg = rows[0]
    assert cfg.id == 1
    assert cfg.roots == ["C:\\Chanjin\\dev"]
    assert cfg.excludes == [
        ".git/",
        "node_modules/",
        ".venv/",
        "venv/",
        "__pycache__/",
        "~$*",
        "*.tmp",
    ]
    assert cfg.max_file_mb == 10
    # 고정 제외는 코드에 두므로 설정에 넣지 않는다
    assert not any("project" in e or "backup" in e for e in cfg.excludes)

    # 단일 행: 두 번째 행은 막힌다
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(SyncConfig(id=2, roots=[], excludes=[]))
            await session.flush()


async def test_status_values_are_restricted(session: AsyncSession) -> None:
    doc = make_document(r"C:\Chanjin\dev\a.txt")
    doc.sections = [Section(seq=0, kind=SectionKind.TEXT, text="본문")]
    session.add(doc)
    await session.flush()
    session.add(
        Job(
            type=JobType.EXTRACT,
            document_id=doc.id,
            sha256=doc.sha256,
            priority=doc.mtime,
        )
    )
    await session.flush()
    status = await session.scalar(select(Job.status))
    assert status == JobStatus.QUEUED

    # TRD에 없는 상태 값은 DB의 CHECK 제약이 막는다
    for sql in (
        "update documents set status = 'done'",
        "update jobs set type = 'summarize'",
        "update sections set kind = 'image'",
    ):
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(text(sql))
