"""문서 API (F-05, F-10, F-19, TRD 5.1, 6.5, 8.3).

- `GET /documents`: 목록. 필터(상태 여러 개, 형식, 경로 앞부분, 검색어, 로컬 삭제 여부),
  페이지네이션, 화면 탭별 개수(`counts`).
- `GET /documents/suggest`: 경로 앞부분 → 한 단계 아래 폴더 후보(채팅 디렉터리 필터 자동완성).
- `GET /documents/{id}`: 메타데이터와 위치별 추출 텍스트(`sections`).
- `POST /documents/{id}/reprocess`: 실패·텍스트 없음은 `needs_upload`(다음 동기화 때 재업로드),
  정상(`ready`·`indexed`)은 sections에서 재청킹(`index` 작업).
- `DELETE /documents/{id}`: 영구 삭제(`app.ingest.purge`, 인용 구절 가림).

로그: `documents.reprocess.requested`(문서 ID, 이전 상태, 처리 방식),
`documents.purged`(원인, 개수만).
경로·파일명·검색어 원문은 남기지 않는다.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from sqlalchemy import ColumnElement, and_, func, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession
from app.api.errors import ApiError
from app.core.auth import utcnow
from app.core.logging import get_logger
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.keys import make_path_key
from app.db.models import Chunk, Document, Job, Section
from app.ingest.purge import purge_documents
from app.jobs.queue import enqueue
from app.query.search import directory_prefix, normalize_exts

router = APIRouter(tags=["documents"])
log = get_logger(__name__)

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200
SUGGEST_LIMIT = 20

# 재처리 방식(TRD 8.3). 실패·텍스트 없음은 원본이 없어(추출 후 삭제) 다시 올려야 한다.
NEEDS_UPLOAD_STATUSES = frozenset({DocumentStatus.FAILED, DocumentStatus.NO_TEXT})
RECHUNK_STATUSES = frozenset({DocumentStatus.READY, DocumentStatus.INDEXED})


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------


class DocumentOut(BaseModel):
    id: int
    path: str
    filename: str
    ext: str
    size: int
    mtime: datetime
    status: str
    failure_code: str | None
    failure_detail: str | None
    locally_deleted: bool
    deleted_at: datetime | None
    needs_upload: bool
    chunks: int
    updated_at: datetime


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int
    page: int
    page_size: int
    # 화면 탭별 개수: 상태별 + `locally_deleted`(다른 필터와 무관한 전체 기준)
    counts: dict[str, int]


class SectionOut(BaseModel):
    seq: int
    location: dict[str, Any]
    heading_path: list[str]
    kind: str
    text: str


class DocumentDetail(DocumentOut):
    sections: list[SectionOut]


class ReprocessOut(BaseModel):
    id: int
    action: Literal["needs_upload", "rechunk"]
    document: DocumentOut


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _chunk_count() -> Any:
    return (
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )


def _out(doc: Document, chunks: int) -> DocumentOut:
    return DocumentOut(
        id=doc.id,
        path=doc.path,
        filename=doc.filename,
        ext=doc.ext,
        size=doc.size,
        mtime=doc.mtime,
        status=doc.status.value,
        failure_code=doc.failure_code.value if doc.failure_code else None,
        failure_detail=doc.failure_detail,
        locally_deleted=doc.locally_deleted,
        deleted_at=doc.deleted_at,
        needs_upload=doc.needs_upload,
        chunks=chunks,
        updated_at=doc.updated_at,
    )


def parse_document_id(raw: str) -> int:
    if not raw.isdigit():
        raise ApiError(404, "document_not_found", "문서를 찾을 수 없습니다")
    return int(raw)


async def _get(db: AsyncSession, document_id: int, *, lock: bool = False) -> Document:
    stmt = select(Document).where(Document.id == document_id)
    if lock:
        stmt = stmt.with_for_update()
    doc = (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()
    if doc is None:
        raise ApiError(404, "document_not_found", "문서를 찾을 수 없습니다")
    return doc


async def _chunks_of(db: AsyncSession, document_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
        )
        or 0
    )


def _filters(
    status: list[str] | None,
    ext: list[str] | None,
    path: str | None,
    q: str | None,
    locally_deleted: bool | None,
) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = []
    if status:
        valid = {s.value for s in DocumentStatus}
        bad = [s for s in status if s not in valid]
        if bad:
            raise ApiError(422, "invalid_status", "알 수 없는 상태입니다", extra={"status": bad})
        conds.append(Document.status.in_([DocumentStatus(s) for s in status]))
    exts = normalize_exts(ext)
    if exts:
        conds.append(func.ltrim(Document.ext, ".").in_(exts))
    prefix = directory_prefix(path)
    if prefix is not None:
        # LIKE는 `\`를 이스케이프 문자로 읽어 Windows 경로에서 틀리므로 starts_with로 비교한다
        conds.append(func.starts_with(Document.path_key, prefix))
    if q and q.strip():
        conds.append(func.strpos(Document.path_key, make_path_key(q.strip())) > 0)
    if locally_deleted is not None:
        conds.append(Document.locally_deleted.is_(locally_deleted))
    return conds


async def _tab_counts(db: AsyncSession) -> dict[str, int]:
    counts = {s.value: 0 for s in DocumentStatus}
    rows = (await db.execute(select(Document.status, func.count()).group_by(Document.status))).all()
    for status, n in rows:
        counts[DocumentStatus(status).value] = int(n)
    counts["locally_deleted"] = int(
        await db.scalar(
            select(func.count()).select_from(Document).where(Document.locally_deleted.is_(True))
        )
        or 0
    )
    return counts


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------


@router.get("/documents")
async def list_documents(
    db: DbSession,
    status: Annotated[list[str] | None, Query(description="상태(여러 개 가능)")] = None,
    ext: Annotated[list[str] | None, Query(description="형식(점 없이도 됨)")] = None,
    path: Annotated[str | None, Query(description="경로 앞부분(폴더, 하위 포함)")] = None,
    q: Annotated[str | None, Query(max_length=200, description="경로·파일명 검색어")] = None,
    locally_deleted: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=PAGE_SIZE_MAX)] = PAGE_SIZE_DEFAULT,
) -> DocumentList:
    conds = _filters(status, ext, path, q, locally_deleted)
    where = and_(true(), *conds)
    total = int(await db.scalar(select(func.count()).select_from(Document).where(where)) or 0)
    rows = (
        await db.execute(
            select(Document, _chunk_count())
            .where(where)
            .order_by(Document.mtime.desc(), Document.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    result = DocumentList(
        items=[_out(doc, int(n)) for doc, n in rows],
        total=total,
        page=page,
        page_size=page_size,
        counts=await _tab_counts(db),
    )
    log.info(
        "documents.listed",
        total=total,
        page=page,
        returned=len(result.items),
        filters=sorted(
            k
            for k, v in {
                "status": status,
                "ext": ext,
                "path": path,
                "q": q,
                "locally_deleted": locally_deleted,
            }.items()
            if v not in (None, [], "")
        ),
    )
    return result


def _parent(path: str) -> str:
    cut = max(path.rfind("\\"), path.rfind("/"))
    return path[:cut] if cut > 0 else ""


@router.get("/documents/suggest")
async def suggest_folders(
    db: DbSession, prefix: Annotated[str, Query(max_length=500)] = ""
) -> list[str]:
    """경로 앞부분 → 폴더 후보. 앞부분 바로 다음 단계까지의 폴더 경로를 원래 표기대로 준다.

    예: `C:\\Chanjin\\de` → `C:\\Chanjin\\dev`, `C:\\Chanjin\\dev\\` → `C:\\Chanjin\\dev\\문서` 등.
    """
    key = make_path_key(prefix.strip()).replace("/", "\\")
    stmt = select(Document.path).distinct()
    if key:
        stmt = stmt.where(func.starts_with(Document.path_key, key))
    paths = (await db.scalars(stmt.limit(5000))).all()
    found: dict[str, str] = {}
    for p in paths:
        folder = _parent(p.replace("/", "\\"))
        if not folder or len(folder) < len(key):
            continue
        if not make_path_key(folder).startswith(key):
            continue
        # 앞부분 다음의 첫 구분자까지만(한 단계 아래 폴더)
        cut = folder.find("\\", max(len(key), 1))
        if cut > 0 and folder[:cut].endswith(":"):  # `C:` 드라이브만 남지 않게
            cut = folder.find("\\", cut + 1)
        candidate = folder if cut < 0 else folder[:cut]
        found.setdefault(make_path_key(candidate), candidate)
    result = [found[k] for k in sorted(found)][:SUGGEST_LIMIT]
    log.info("documents.suggested", prefix_chars=len(prefix), returned=len(result))
    return result


@router.get("/documents/{document_id}")
async def read_document(document_id: str, db: DbSession) -> DocumentDetail:
    doc = await _get(db, parse_document_id(document_id))
    sections = (
        await db.scalars(select(Section).where(Section.document_id == doc.id).order_by(Section.seq))
    ).all()
    detail = DocumentDetail(
        **_out(doc, await _chunks_of(db, doc.id)).model_dump(),
        sections=[
            SectionOut(
                seq=s.seq,
                location=dict(s.location),
                heading_path=list(s.heading_path),
                kind=s.kind.value,
                text=s.text,
            )
            for s in sections
        ],
    )
    log.info("documents.read", document_id=doc.id, sections=len(detail.sections))
    return detail


@router.post("/documents/{document_id}/reprocess")
async def reprocess_document(document_id: str, db: DbSession) -> ReprocessOut:
    doc = await _get(db, parse_document_id(document_id), lock=True)
    previous = doc.status
    action: Literal["needs_upload", "rechunk"]
    has_sections = previous in RECHUNK_STATUSES and bool(
        await db.scalar(
            select(func.count()).select_from(Section).where(Section.document_id == doc.id)
        )
    )
    if previous in NEEDS_UPLOAD_STATUSES or (previous in RECHUNK_STATUSES and not has_sections):
        # 원본은 추출 뒤 지웠으므로 다음 동기화 때 Agent가 다시 올리게 한다(manifest 판정 `upload`)
        doc.needs_upload = True
        action = "needs_upload"
    elif previous in RECHUNK_STATUSES:
        now = utcnow()
        # 기다리는 index·embed 작업은 새 재청킹으로 대신한다(실행 중인 작업은 없음: 상태로 거름)
        await db.execute(
            update(Job)
            .where(
                Job.document_id == doc.id,
                Job.status == JobStatus.QUEUED,
                Job.type.in_([JobType.INDEX, JobType.EMBED]),
            )
            .values(status=JobStatus.DONE, last_error="재처리 요청으로 대체", updated_at=now)
        )
        doc.status = DocumentStatus.EXTRACTING
        await enqueue(db, type=JobType.INDEX, document=doc, run_after=now)
        action = "rechunk"
    else:
        raise ApiError(
            409,
            "not_reprocessable",
            "지금은 재처리할 수 없는 상태입니다",
            extra={"status": previous.value},
        )
    await db.commit()
    doc = await _get(db, doc.id)
    log.info(
        "documents.reprocess.requested",
        document_id=doc.id,
        previous_status=previous.value,
        action=action,
        failure_code=doc.failure_code.value if doc.failure_code else None,
    )
    return ReprocessOut(id=doc.id, action=action, document=_out(doc, await _chunks_of(db, doc.id)))


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: str, db: DbSession) -> Response:
    doc = await _get(db, parse_document_id(document_id), lock=True)
    await purge_documents(db, [doc.id], reason="user")
    await db.commit()
    return Response(status_code=204)
