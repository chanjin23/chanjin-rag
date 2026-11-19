"""문서 접수: 업로드 원본을 임시 폴더에 두고 `extract` 작업을 등록한다 (TRD 6.1, 6.2).

Sync API의 업로드(T07)와 개발용 `rag-backend ingest-local`이 같은 함수를 쓴다.

- 문서 행은 `path_key`(NFC + 소문자)로 찾는다. 해시가 같고 재처리 요청(`needs_upload`)이 없으면
  아무것도 하지 않는다(수정 시각과 경로 표기만 갱신).
- 새 버전이면 상태를 `queued`로 되돌리고 실패 사유를 지운다. 진행 중이던 이전 버전 작업은 끝날 때
  `jobs.sha256`이 달라 결과를 버린다.
- 임시 원본: `UPLOAD_TMP_DIR/{document_id}-{sha256}{ext}`. `extract`가 파싱 후(실패해도) 지운다.
- 10MB 초과(`meta_only`)는 원본 없이 파일명 청크 1개만 만든다(BM25 파일명 검색).
"""

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.enums import DocumentStatus, JobType
from app.db.keys import make_path_key
from app.db.models import Chunk, Document, Section
from app.ingest.chunking import filename_chunk
from app.ingest.chunking.chunker import Chunk as ChunkData
from app.jobs.queue import enqueue

IntakeOutcome = Literal["queued", "unchanged", "meta_only"]


@dataclass(frozen=True)
class FileMeta:
    """Agent manifest 한 항목과 같은 값. `path`는 원래 경로(Windows 경로 그대로)."""

    path: str
    size: int
    mtime: datetime
    sha256: str

    @property
    def filename(self) -> str:
        return PureWindowsPath(self.path).name if "\\" in self.path else Path(self.path).name

    @property
    def ext(self) -> str:
        dot = self.filename.rfind(".")
        return self.filename[dot:].lower() if dot >= 0 else ""


@dataclass(frozen=True)
class IntakeResult:
    document_id: int
    outcome: IntakeOutcome


def original_path(document_id: int, sha256: str, ext: str) -> Path:
    """임시 원본 위치. 버전(해시)마다 다른 이름이라 새 버전이 이전 파일을 덮지 않는다."""
    return get_settings().upload_tmp_path / f"{document_id}-{sha256}{ext}"


def remove_originals(document_id: int, *, keep: Path | None = None) -> None:
    """문서의 임시 원본을 지운다(`keep`은 남김). 영구 삭제·새 버전 접수 때 쓴다."""
    folder = get_settings().upload_tmp_path
    if not folder.is_dir():
        return
    for f in folder.glob(f"{document_id}-*"):
        if keep is None or f != keep:
            _unlink_quietly(f)


def remove_original_version(document_id: int, sha256: str) -> None:
    """특정 버전의 임시 원본만 지운다(지난 버전 extract 작업을 버릴 때)."""
    folder = get_settings().upload_tmp_path
    if folder.is_dir():
        for f in folder.glob(f"{document_id}-{sha256}*"):
            _unlink_quietly(f)


def _unlink_quietly(path: Path) -> None:
    # Windows에서는 파싱 중인(열린) 파일을 지울 수 없다. 그 파일은 extract가 끝나며 지운다.
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def _find(session: AsyncSession, path: str) -> Document | None:
    return await session.scalar(
        select(Document).where(Document.path_key == make_path_key(path)).with_for_update()
    )


def _apply_version(doc: Document, meta: FileMeta, status: DocumentStatus) -> None:
    doc.path = meta.path
    doc.filename = meta.filename
    doc.ext = meta.ext
    doc.size = meta.size
    doc.mtime = meta.mtime
    doc.sha256 = meta.sha256
    doc.status = status
    doc.failure_code = None
    doc.failure_detail = None
    doc.needs_upload = False
    doc.locally_deleted = False
    doc.deleted_at = None


async def _upsert(
    session: AsyncSession, meta: FileMeta, status: DocumentStatus
) -> tuple[Document, bool]:
    """(문서, 바뀌었는지). 해시가 같고 재처리 요청이 없으면 바뀌지 않은 것."""
    doc = await _find(session, meta.path)
    if doc is not None and doc.sha256 == meta.sha256 and not doc.needs_upload:
        doc.mtime = meta.mtime
        doc.path = meta.path
        return doc, False
    if doc is None:
        doc = Document(path_key=meta.path, path=meta.path, filename="", ext="", size=0)
        session.add(doc)
    _apply_version(doc, meta, status)
    await session.flush()
    return doc, True


async def accept_upload(
    session: AsyncSession, meta: FileMeta, source: Path, *, move: bool = False
) -> IntakeResult:
    """업로드 파일(`source`)을 접수한다. 새 버전이면 임시 원본을 두고 `extract` 작업을 등록한다.

    commit은 호출한 쪽이 한다. `move=True`면 `source`를 옮기고, 아니면 복사한다.
    """
    doc, changed = await _upsert(session, meta, DocumentStatus.QUEUED)
    if not changed:
        return IntakeResult(doc.id, "unchanged")
    target = original_path(doc.id, meta.sha256, meta.ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(source, target)
    else:
        shutil.copyfile(source, target)
    remove_originals(doc.id, keep=target)
    await enqueue(session, type=JobType.EXTRACT, document=doc)
    return IntakeResult(doc.id, "queued")


def chunk_row(document: Document, data: ChunkData) -> Chunk:
    """청크 데이터(T05)를 DB 행으로. `filename`은 문서 파일명 사본(BM25 색인용)."""
    return Chunk(
        document_id=document.id,
        seq=data.seq,
        location=data.location,
        heading_path=list(data.heading_path),
        filename=document.filename,
        content=data.content,
        content_sha256=data.content_sha256,
        bm25_text=data.bm25_text,
        token_count=data.token_count,
    )


async def replace_with_filename_chunk(session: AsyncSession, document: Document) -> None:
    """섹션·청크를 지우고 파일명 청크 1개만 둔다(`meta_only`, `no_text`, `unsupported_format`)."""
    await session.execute(delete(Section).where(Section.document_id == document.id))
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    session.add(chunk_row(document, filename_chunk(document.filename, document.path)))
    document.content_indexed = True
    await session.flush()


async def accept_meta_only(session: AsyncSession, meta: FileMeta) -> IntakeResult:
    """10MB 초과 파일: 내용 없이 파일명만 색인한다(원본을 받지 않음)."""
    doc, changed = await _upsert(session, meta, DocumentStatus.META_ONLY)
    if not changed and doc.status == DocumentStatus.META_ONLY:
        return IntakeResult(doc.id, "unchanged")
    doc.status = DocumentStatus.META_ONLY
    remove_originals(doc.id)
    await replace_with_filename_chunk(session, doc)
    return IntakeResult(doc.id, "meta_only")
