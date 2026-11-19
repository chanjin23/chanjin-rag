"""manifest 판정의 이동·복사 처리 (TRD 6.1 판정표). 둘 다 업로드와 임베딩 한도를 쓰지 않는다.

- 이동: 기존 문서의 경로만 바꾼다. 청크의 경로 관련 값(`chunks.filename`, 파일명 청크의 내용과
  BM25 토큰)도 함께 바꾼다. `content_sha256`은 저장된 임베딩의 실제 입력 해시라 그대로 둔다.
- 복사: 같은 해시의 기존 문서에서 sections·chunks·임베딩을 새 문서로 복사한다. 원본의 임베딩이
  끝났으면(`ready` 등) API 호출도 한도도 쓰지 않는다.
- 원본이 아직 임베딩 전·중(`indexed`·`embedding`)이면 복사할 벡터가 (일부) 없다. 이때 복사본은
  `indexed` + 일반 `embed` 작업으로 등록하고 `embed_charged_sha256`을 비워 둔다. 남은 청크를
  임베딩하려면 API를 실제로 부르므로, 다른 파일과 똑같이 하루 파일 한도를 1 쓴다(한도가 차면 다음
  날로 미뤄짐). 한도를 건너뛰게 표시하면 하루 한도를 넘겨 API를 부를 수 있다.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import DocumentStatus, JobType
from app.db.models import Chunk, Document, Section
from app.ingest.chunking import filename_chunk
from app.ingest.chunking.chunker import Chunk as ChunkData
from app.ingest.intake import FileMeta
from app.jobs.queue import enqueue

# 추출이 끝나지 않은 상태. 이런 문서는 복사할 내용이 아직 없어 새 경로를 업로드로 판정한다.
_NOT_EXTRACTED = frozenset({DocumentStatus.QUEUED, DocumentStatus.EXTRACTING})


def copyable(doc: Document) -> bool:
    return doc.content_indexed and not doc.needs_upload and doc.status not in _NOT_EXTRACTED


def _is_filename_chunk(chunk: Chunk, meta_chunk: ChunkData) -> bool:
    """파일명 청크(meta_only·no_text·미지원 형식)인지. 내용에 파일명과 경로가 들어 있다."""
    return chunk.content == meta_chunk.content and not chunk.location and not chunk.heading_path


def _apply_filename_chunk(chunk: Chunk, meta_chunk: ChunkData) -> None:
    chunk.content = meta_chunk.content
    chunk.content_sha256 = meta_chunk.content_sha256
    chunk.bm25_text = meta_chunk.bm25_text
    chunk.token_count = meta_chunk.token_count


async def relocate(session: AsyncSession, doc: Document, meta: FileMeta) -> None:
    """문서를 새 경로로 옮긴다(이동, 또는 대소문자·정규화만 다른 경로 표기 변경)."""
    old_filename, old_path = doc.filename, doc.path
    old_meta = filename_chunk(old_filename, old_path)
    doc.path = meta.path
    doc.path_key = meta.path
    doc.filename = meta.filename
    doc.ext = meta.ext
    doc.mtime = meta.mtime
    if old_filename == doc.filename and old_path == doc.path:
        return
    new_meta = filename_chunk(doc.filename, doc.path)
    chunks = (await session.scalars(select(Chunk).where(Chunk.document_id == doc.id))).all()
    for chunk in chunks:
        chunk.filename = doc.filename
        if _is_filename_chunk(chunk, old_meta):
            _apply_filename_chunk(chunk, new_meta)
    await session.flush()


async def copy_document(
    session: AsyncSession, source: Document, meta: FileMeta, now: datetime
) -> Document:
    """`source`의 추출 결과를 새 경로의 문서로 복사한다(업로드 없음).

    원본 임베딩이 끝났으면 한도를 쓰지 않는다. 원본이 임베딩 전·중이면 복사본은 일반 `embed`
    작업으로 남은 청크를 임베딩하고 그때 하루 파일 한도를 쓴다(모듈 설명 참고).
    """
    status = source.status
    pending_embed = status in (DocumentStatus.INDEXED, DocumentStatus.EMBEDDING)
    if status == DocumentStatus.EMBEDDING:
        status = DocumentStatus.INDEXED
    copy = Document(
        path=meta.path,
        path_key=meta.path,
        filename=meta.filename,
        ext=meta.ext,
        size=meta.size,
        mtime=meta.mtime,
        sha256=meta.sha256,
        status=status,
        failure_code=source.failure_code,
        failure_detail=source.failure_detail,
        content_indexed=source.content_indexed,
        # 임베딩이 끝난 원본의 복사본만 "이미 차감"으로 둔다(남은 API 호출이 없음)
        embed_charged_sha256=None if pending_embed else meta.sha256,
        embed_model=source.embed_model,
        embed_dim=source.embed_dim,
    )
    session.add(copy)
    await session.flush()

    sections = (
        await session.scalars(select(Section).where(Section.document_id == source.id))
    ).all()
    for s in sections:
        session.add(
            Section(
                document_id=copy.id,
                seq=s.seq,
                location=dict(s.location),
                heading_path=list(s.heading_path),
                kind=s.kind,
                text=s.text,
            )
        )
    old_meta = filename_chunk(source.filename, source.path)
    new_meta = filename_chunk(copy.filename, copy.path)
    chunks = (await session.scalars(select(Chunk).where(Chunk.document_id == source.id))).all()
    for c in chunks:
        row = Chunk(
            document_id=copy.id,
            seq=c.seq,
            location=dict(c.location),
            heading_path=list(c.heading_path),
            filename=copy.filename,
            content=c.content,
            content_sha256=c.content_sha256,
            bm25_text=c.bm25_text,
            token_count=c.token_count,
            embedding=c.embedding,
            embed_model=c.embed_model,
        )
        if _is_filename_chunk(c, old_meta):
            _apply_filename_chunk(row, new_meta)
        session.add(row)
    await session.flush()
    if pending_embed:
        # 남은 청크(벡터가 없는 것)만 임베딩한다. claim_embed가 하루 파일 한도를 확인·차감한다
        await enqueue(session, type=JobType.EMBED, document=copy, run_after=now)
    return copy
