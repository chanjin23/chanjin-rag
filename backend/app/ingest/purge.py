"""문서 영구 삭제 (F-19, TRD 6.5).

- 범위: `documents`, `sections`, `chunks`, 관련 `jobs`(외래 키 ON DELETE CASCADE),
  `UPLOAD_TMP_DIR`의 임시 원본(`{id}-*`).
- 예전 대화의 인용: 사용자 요청·범위 제외로 지운 문서는 프라이버시 때문에 지운 것이므로
  `messages.citations`에서 그 문서 항목의 인용 구절(`snippet`)을 "삭제된 문서"로 바꾸고
  `deleted: true`를 붙인다(`mask=True`). 로컬 삭제 30일 경과(`retention`)는 인용을 그대로
  둔다.
- 질의 로그(`query_logs.retrieved`)는 청크 ID와 점수만 있으므로 그대로 둔다.
- 처리 중인 작업은 끝날 때 작업 행이 없어 결과를 버린다(`JobGone`).
- 로그 `documents.purged`: 원인과 개수만(경로·파일명 없음).

사용자 요청(`DELETE /documents/{id}`, T12)이 쓰고,
범위 제외(T13)·30일 자동 삭제(T14)도 같은 함수를 쓴다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Chunk, Document, Job, Message, Section
from app.ingest.intake import remove_originals

log = get_logger(__name__)

PurgeReason = Literal["user", "scope", "retention"]

DELETED_SNIPPET = "삭제된 문서"


@dataclass(frozen=True)
class PurgeResult:
    documents: int
    sections: int
    chunks: int
    jobs: int
    masked_messages: int

    def as_dict(self) -> dict[str, int]:
        return {
            "documents": self.documents,
            "sections": self.sections,
            "chunks": self.chunks,
            "jobs": self.jobs,
            "masked_messages": self.masked_messages,
        }


async def _count(session: AsyncSession, model: Any, ids: Sequence[int]) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(model).where(model.document_id.in_(list(ids)))
        )
        or 0
    )


def mask_citation_list(
    citations: list[dict[str, Any]], ids: set[int]
) -> tuple[list[dict[str, Any]], bool]:
    """인용 목록에서 지운 문서 항목의 구절을 가린다. (새 목록, 바뀌었는지)."""
    changed = False
    out: list[dict[str, Any]] = []
    for c in citations:
        doc_id = c.get("document_id") if isinstance(c, dict) else None
        if isinstance(doc_id, int) and doc_id in ids:
            if c.get("snippet") != DELETED_SNIPPET or not c.get("deleted"):
                changed = True
            out.append({**c, "snippet": DELETED_SNIPPET, "deleted": True})
        else:
            out.append(c)
    return out, changed


async def mask_citations(session: AsyncSession, ids: Sequence[int]) -> int:
    """지운 문서를 인용한 메시지의 인용 구절을 "삭제된 문서"로 바꾼다. 바꾼 메시지 수."""
    if not ids:
        return 0
    rows = (
        await session.execute(
            select(Message.id, Message.citations).where(
                text(
                    "EXISTS (SELECT 1 FROM jsonb_array_elements(messages.citations) e "
                    "WHERE jsonb_typeof(e->'document_id') = 'number' "
                    "AND (e->>'document_id')::bigint = ANY(:purge_ids))"
                ).bindparams(purge_ids=list(ids))
            )
        )
    ).all()
    wanted = set(ids)
    masked = 0
    for message_id, citations in rows:
        new, changed = mask_citation_list(list(citations or []), wanted)
        if changed:
            await session.execute(
                update(Message).where(Message.id == message_id).values(citations=new)
            )
            masked += 1
    return masked


async def purge_documents(
    session: AsyncSession,
    ids: Sequence[int],
    *,
    reason: PurgeReason,
    mask: bool | None = None,
) -> PurgeResult:
    """문서를 영구 삭제한다. commit은 호출한 쪽이 하고, 임시 원본은 여기서 지운다.

    `mask`를 주지 않으면 원인에 따라 정한다(사용자·범위 제외는 인용을 가리고, 30일 경과는 둔다).
    """
    found = list(
        (await session.scalars(select(Document.id).where(Document.id.in_(list(ids))))).all()
    )
    if mask is None:
        mask = reason in ("user", "scope")
    if not found:
        result = PurgeResult(0, 0, 0, 0, 0)
    else:
        sections = await _count(session, Section, found)
        chunks = await _count(session, Chunk, found)
        jobs = await _count(session, Job, found)
        masked = await mask_citations(session, found) if mask else 0
        # sections·chunks·jobs는 외래 키 ON DELETE CASCADE로 함께 지워진다
        await session.execute(delete(Document).where(Document.id.in_(found)))
        await session.flush()
        for doc_id in found:
            remove_originals(doc_id)
        result = PurgeResult(len(found), sections, chunks, jobs, masked)
    log.info("documents.purged", reason=reason, **result.as_dict())
    return result
