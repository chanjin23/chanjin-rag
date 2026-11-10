"""질의 그래프·채팅 SSE 테스트 공용 도우미 (test_graph.py, test_chat_sse.py).

합성 문서를 실제 수집 경로와 같은 모양(sections + chunks, Kiwi BM25 토큰, Fake 임베딩)으로 넣는다.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import DocumentStatus, FailureCode, SectionKind
from app.db.models import EMBED_DIM, Chunk, Document, Section
from app.ingest.tokenize import bm25_text
from app.providers.fake import FakeEmbeddingProvider, FakeLLMProvider, FakeRerankProvider
from app.query.fake_llm import fake_query_responder

SHA = "b" * 64
EMBEDDER = FakeEmbeddingProvider(dim=EMBED_DIM)


async def seed_doc(
    session: AsyncSession,
    path: str,
    paragraphs: Sequence[str] = (),
    *,
    status: DocumentStatus = DocumentStatus.READY,
    embed: bool = True,
    mtime: datetime = datetime(2026, 11, 1, tzinfo=UTC),
    failure_code: FailureCode | None = None,
    chunks: bool = True,
) -> Document:
    """문단마다 section 1개와 chunk 1개를 만든다(임베딩은 Fake로 본문에서 계산)."""
    filename = path.replace("/", "\\").rsplit("\\", 1)[-1]
    doc = Document(
        path=path,
        path_key=path,
        filename=filename,
        ext="." + filename.rsplit(".", 1)[-1],
        size=100,
        mtime=mtime,
        sha256=SHA,
        status=status,
        failure_code=failure_code,
    )
    doc.sections = [
        Section(
            seq=i,
            location={"lines": [i * 2 + 1, i * 2 + 1]},
            heading_path=[],
            kind=SectionKind.TEXT,
            text=p,
        )
        for i, p in enumerate(paragraphs)
    ]
    if chunks:
        doc.chunks = [
            Chunk(
                seq=i,
                location={"lines": [i * 2 + 1, i * 2 + 1]},
                heading_path=[],
                filename=filename,
                content=p,
                content_sha256=SHA,
                bm25_text=bm25_text(p),
                token_count=len(p),
                embedding=EMBEDDER.vector(p) if embed else None,
                embed_model=EMBEDDER.model if embed else None,
            )
            for i, p in enumerate(paragraphs)
        ]
    session.add(doc)
    await session.flush()
    return doc


async def seed_corpus(session: AsyncSession) -> dict[str, Document]:
    """질문 테스트용 문서 4개."""
    return {
        "contract": await seed_doc(
            session,
            r"C:\docs\계약\계약서.md",
            ["위약금은 계약 금액의 10%로 한다.", "계약 기간은 2026년 1월부터 12월까지이다."],
        ),
        "project": await seed_doc(
            session,
            r"C:\docs\프로젝트\알파.md",
            ["알파 프로젝트의 마감일은 12월 15일이다.", "알파 프로젝트 담당자는 기획팀이다."],
        ),
        "meeting": await seed_doc(
            session,
            r"C:\docs\회의\회의록.txt",
            ["주간 회의는 매주 월요일 10시에 한다.", "다음 배포는 금요일에 한다."],
        ),
        "lunch": await seed_doc(
            session, r"C:\docs\메모\점심.md", ["점심 메뉴는 김치찌개와 비빔밥이다."]
        ),
    }


def fake_llm(**kwargs: Any) -> FakeLLMProvider:
    kwargs.setdefault("responder", fake_query_responder)
    return FakeLLMProvider(**kwargs)


def fake_embedder() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(dim=EMBED_DIM)


def fake_reranker() -> FakeRerankProvider:
    return FakeRerankProvider()


def parse_sse(text: str) -> list[tuple[str, Any]]:
    """SSE 본문 → (이벤트 이름, JSON 데이터) 목록. 주석(ping)은 건너뛴다."""
    events: list[tuple[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name = "message"
        data: list[str] = []
        for line in block.split("\n"):
            if line.startswith(":") or not line:
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events
