"""파일 단위 요약의 대상 판정 (PRD F-09, TRD 7.1 "요약 대상 판정").

대상 후보는 다음 순서로 찾는다.

1. 요청의 `summarize_document_id`: 출처 패널의 "요약" 버튼이나 후보 목록에서 고른 문서
   (명시적 선택이라 가장 앞에 둔다).
2. 질문에 나온 파일명: 문서 파일명(확장자 포함)이 질문에 들어 있으면 그 문서들. 없으면 확장자를 뺀
   이름(2자 이상)이 들어 있는 문서들.
3. 직전 답변의 출처("그 문서 요약해줘"): 직전 답변이 인용한 문서들.

| 경우 | 결과 |
| --- | --- |
| 후보 1개, `indexed`·`embedding`·`ready` | `ready`(요약 진행) |
| 후보 여러 개 | `multiple`(후보 목록을 보여주고 고르게 함) |
| 후보 없음 | `none`("해당 파일을 찾지 못했습니다" + 비슷한 파일명 3개) |
| 후보가 `queued`·`extracting` | `processing`("아직 처리 중입니다") |
| 후보가 `meta_only`·`no_text`·`failed` | `no_text`(요약할 텍스트가 없는 이유) |

`embedding`은 추출·청킹이 끝나 sections가 있는 상태(임베딩만 진행 중)라 요약할 수 있다.
"""

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import DocumentStatus, FailureCode
from app.db.models import Document
from app.ingest.parsers.base import FAILURE_GUIDANCE

MAX_CANDIDATES = 10
SIMILAR_COUNT = 3
MIN_STEM_CHARS = 2

SUMMARIZABLE = (DocumentStatus.INDEXED, DocumentStatus.EMBEDDING, DocumentStatus.READY)
PROCESSING = (DocumentStatus.QUEUED, DocumentStatus.EXTRACTING)

# 요약 의도 판정(이력이 없어 LLM 재작성을 건너뛸 때)
SUMMARIZE_RE = re.compile(r"요약|summar", re.IGNORECASE)
_SUMMARY_WORDS_RE = re.compile(
    r"(요약|정리|해\s*줘|해\s*주세요|해줄래|부탁|파일|문서|내용|전체|summarize|summary|please)",
    re.IGNORECASE,
)


class TargetOutcome(StrEnum):
    READY = "ready"
    MULTIPLE = "multiple"
    NONE = "none"
    PROCESSING = "processing"
    NO_TEXT = "no_text"


@dataclass(frozen=True)
class TargetDoc:
    id: int
    path: str
    filename: str
    mtime: datetime
    status: DocumentStatus
    failure_code: FailureCode | None
    failure_detail: str | None
    locally_deleted: bool


@dataclass(frozen=True)
class SummaryTarget:
    outcome: TargetOutcome
    documents: list[TargetDoc] = field(default_factory=list)
    similar: list[str] = field(default_factory=list)
    # 대상을 찾은 방법: explicit | filename | previous | none
    found_by: str = "none"

    @property
    def document(self) -> TargetDoc | None:
        return self.documents[0] if len(self.documents) == 1 else None

    def message(self) -> str:
        """`ready`가 아닌 경우 사용자에게 보여줄 안내 문구."""
        if self.outcome == TargetOutcome.MULTIPLE:
            return (
                f"요약할 수 있는 문서가 {len(self.documents)}개 있습니다. "
                "출처 목록에서 요약할 문서를 골라 주세요."
            )
        if self.outcome == TargetOutcome.NONE:
            text = "해당 파일을 찾지 못했습니다."
            if self.similar:
                text += " 비슷한 이름의 파일: " + ", ".join(self.similar)
            return text
        doc = self.documents[0] if self.documents else None
        name = doc.filename if doc else "문서"
        if self.outcome == TargetOutcome.PROCESSING:
            return f"{name}은(는) 아직 처리 중입니다. 처리가 끝난 뒤 다시 요청해 주세요."
        if self.outcome == TargetOutcome.NO_TEXT and doc is not None:
            return f"{name}은(는) 요약할 텍스트가 없습니다. {no_text_reason(doc)}"
        return ""


def no_text_reason(doc: TargetDoc) -> str:
    if doc.status == DocumentStatus.META_ONLY:
        return "10MB를 넘는 파일이라 파일명만 색인되어 있습니다."
    if doc.status == DocumentStatus.NO_TEXT:
        return "문서에서 텍스트를 추출하지 못했습니다(스캔 이미지 등)."
    if doc.failure_code is not None:
        guide = FAILURE_GUIDANCE.get(doc.failure_code, "")
        return f"처리에 실패했습니다(사유: {doc.failure_code.value}). {guide}".strip()
    detail = f"(사유: {doc.failure_detail[:100]})" if doc.failure_detail else ""
    return f"처리에 실패했습니다{detail}."


def wants_summary(question: str) -> bool:
    return bool(SUMMARIZE_RE.search(question))


def _target(doc: Document) -> TargetDoc:
    return TargetDoc(
        id=doc.id,
        path=doc.path,
        filename=doc.filename,
        mtime=doc.mtime,
        status=DocumentStatus(doc.status),
        failure_code=FailureCode(doc.failure_code) if doc.failure_code else None,
        failure_detail=doc.failure_detail,
        locally_deleted=doc.locally_deleted,
    )


def _stem(filename: str) -> str:
    return filename.rsplit(".", 1)[0] if "." in filename else filename


def judge(documents: Sequence[TargetDoc], found_by: str) -> SummaryTarget:
    """후보 목록 → 판정(TRD 7.1 표)."""
    if not documents:
        return SummaryTarget(TargetOutcome.NONE, found_by=found_by)
    if len(documents) > 1:
        ordered = sorted(documents, key=lambda d: d.mtime, reverse=True)[:MAX_CANDIDATES]
        return SummaryTarget(TargetOutcome.MULTIPLE, ordered, found_by=found_by)
    doc = documents[0]
    if doc.status in SUMMARIZABLE:
        outcome = TargetOutcome.READY
    elif doc.status in PROCESSING:
        outcome = TargetOutcome.PROCESSING
    else:
        outcome = TargetOutcome.NO_TEXT
    return SummaryTarget(outcome, [doc], found_by=found_by)


async def _by_ids(session: AsyncSession, ids: Sequence[int]) -> list[TargetDoc]:
    if not ids:
        return []
    rows = (await session.execute(select(Document).where(Document.id.in_(list(ids))))).scalars()
    by_id = {d.id: _target(d) for d in rows}
    return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]


async def _by_filename(session: AsyncSession, question: str) -> list[TargetDoc]:
    q = question.lower()
    full = (
        await session.execute(
            select(Document).where(func.strpos(q, func.lower(Document.filename)) > 0)
        )
    ).scalars()
    found = [_target(d) for d in full]
    if found:
        # 이름이 더 긴(구체적인) 파일명이 다른 파일명을 포함하면 긴 쪽만 남긴다("a.md" ⊂ "data.md")
        longest = max(len(d.filename) for d in found)
        return [d for d in found if len(d.filename) == longest]
    stem_expr = func.regexp_replace(func.lower(Document.filename), r"\.[^.]*$", "")
    stems = (
        await session.execute(
            select(Document).where(
                func.char_length(stem_expr) >= MIN_STEM_CHARS, func.strpos(q, stem_expr) > 0
            )
        )
    ).scalars()
    found = [_target(d) for d in stems]
    if found:
        longest = max(len(_stem(d.filename)) for d in found)
        return [d for d in found if len(_stem(d.filename)) == longest]
    return []


async def similar_filenames(
    session: AsyncSession, question: str, limit: int = SIMILAR_COUNT
) -> list[str]:
    """질문에서 요약 관련 낱말을 뺀 나머지와 가장 비슷한 파일명."""
    probe = _SUMMARY_WORDS_RE.sub(" ", question.lower())
    probe = re.sub(r"[^\w.\-]+", " ", probe).strip()
    names = list((await session.execute(select(Document.filename).distinct())).scalars())
    if not probe or not names:
        return []

    def score(name: str) -> float:
        lower = name.lower()
        whole = difflib.SequenceMatcher(None, probe, lower).ratio()
        best_word = max(
            (difflib.SequenceMatcher(None, w, _stem(lower)).ratio() for w in probe.split()),
            default=0.0,
        )
        return max(whole, best_word)

    ranked = sorted(names, key=lambda n: (-score(n), n))
    return ranked[:limit]


async def resolve_summary_target(
    session: AsyncSession,
    question: str,
    *,
    summarize_document_id: int | None = None,
    previous_document_ids: Sequence[int] = (),
) -> SummaryTarget:
    if summarize_document_id is not None:
        return judge(await _by_ids(session, [summarize_document_id]), "explicit")
    by_name = await _by_filename(session, question)
    if by_name:
        return judge(by_name, "filename")
    previous = await _by_ids(session, previous_document_ids)
    if previous:
        return judge(previous, "previous")
    similar = await similar_filenames(session, question)
    return SummaryTarget(TargetOutcome.NONE, similar=similar, found_by="none")
