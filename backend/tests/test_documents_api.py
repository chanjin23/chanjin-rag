"""문서 API (F-05, F-10, F-19, TRD 5.1, 6.5, 8.3).

테스트 DB 세션(끝나면 롤백) + Fake Provider. 문서는 합성 데이터(`chat_seed.seed_doc`)다.
재청킹은 실제 Worker 코드(`JobRunner`)를 같은 연결 위의 세션 묶음으로 돌려 확인한다.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from chat_seed import EMBEDDER, parse_sse, seed_corpus, seed_doc
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.db.enums import DocumentStatus, FailureCode, JobStatus, JobType, SyncRunSource
from app.db.models import Chunk, Document, Job, Message, Section
from app.ingest.intake import original_path
from app.ingest.purge import DELETED_SNIPPET, purge_documents
from app.jobs.queue import enqueue
from app.jobs.runner import JobRunner
from app.providers.fake import FakeEmbeddingProvider
from app.query.search import hybrid_search


async def count(session: AsyncSession, model: Any, *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def ids(res: httpx.Response) -> list[int]:
    assert res.status_code == 200, res.text
    return [d["id"] for d in res.json()["items"]]


async def test_list_filters_and_pagination(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    d = {
        "ready": await seed_doc(
            session,
            r"C:\docs\보고서\주간보고.md",
            ["첫 문단"],
            mtime=datetime(2026, 11, 5, tzinfo=UTC),
        ),
        "failed": await seed_doc(
            session,
            r"C:\docs\보고서\암호.pdf",
            status=DocumentStatus.FAILED,
            failure_code=FailureCode.ENCRYPTED,
            mtime=datetime(2026, 11, 4, tzinfo=UTC),
        ),
        "indexed": await seed_doc(
            session,
            r"C:\docs\메모\할일.txt",
            ["할 일"],
            status=DocumentStatus.INDEXED,
            embed=False,
            mtime=datetime(2026, 11, 3, tzinfo=UTC),
        ),
        "extracting": await seed_doc(
            session,
            r"C:\docs\메모\회의.docx",
            status=DocumentStatus.EXTRACTING,
            chunks=False,
            mtime=datetime(2026, 11, 2, tzinfo=UTC),
        ),
        "embedding": await seed_doc(
            session,
            r"C:\docs\발표\발표.pptx",
            ["슬라이드"],
            status=DocumentStatus.EMBEDDING,
            embed=False,
            mtime=datetime(2026, 11, 1, tzinfo=UTC),
        ),
    }
    d["ready"].locally_deleted = True
    d["ready"].deleted_at = datetime(2026, 11, 6, tzinfo=UTC)
    await session.flush()

    # 전체: 수정 시각 최신순, 항목 필드
    res = await chat_client.get("/api/documents")
    assert ids(res) == [d[k].id for k in ("ready", "failed", "indexed", "extracting", "embedding")]
    body = res.json()
    assert body["total"] == 5 and body["page"] == 1
    first = body["items"][0]
    assert first["path"] == r"C:\docs\보고서\주간보고.md"
    assert first["filename"] == "주간보고.md"
    assert first["locally_deleted"] is True and first["chunks"] == 1
    failed = body["items"][1]
    assert failed["status"] == "failed" and failed["failure_code"] == "encrypted"
    assert failed["needs_upload"] is False
    # 탭별 개수(상태별 + 로컬 삭제)
    assert body["counts"]["ready"] == 1
    assert body["counts"]["failed"] == 1
    assert body["counts"]["extracting"] == 1 and body["counts"]["embedding"] == 1
    assert body["counts"]["queued"] == 0 and body["counts"]["meta_only"] == 0
    assert body["counts"]["locally_deleted"] == 1

    # 상태(여러 개 = "처리 중" 탭), 형식, 경로 앞부분, 검색어, 로컬 삭제
    processing = await chat_client.get(
        "/api/documents", params=[("status", "extracting"), ("status", "embedding")]
    )
    assert ids(processing) == [d["extracting"].id, d["embedding"].id]
    assert ids(await chat_client.get("/api/documents", params={"status": "failed"})) == [
        d["failed"].id
    ]
    assert ids(await chat_client.get("/api/documents", params={"ext": "PDF"})) == [d["failed"].id]
    assert ids(
        await chat_client.get("/api/documents", params=[("ext", ".md"), ("ext", "txt")])
    ) == [d["ready"].id, d["indexed"].id]
    # 경로 앞부분: 폴더 경계로 자르고 대소문자를 무시한다(`C:\docs\메` 는 폴더가 아니므로 0개)
    assert ids(await chat_client.get("/api/documents", params={"path": "c:\\DOCS\\메모"})) == [
        d["indexed"].id,
        d["extracting"].id,
    ]
    assert ids(await chat_client.get("/api/documents", params={"path": "C:\\docs\\메"})) == []
    assert ids(await chat_client.get("/api/documents", params={"q": "보고"})) == [
        d["ready"].id,
        d["failed"].id,
    ]
    assert ids(await chat_client.get("/api/documents", params={"q": "PPTX"})) == [d["embedding"].id]
    assert ids(await chat_client.get("/api/documents", params={"locally_deleted": "true"})) == [
        d["ready"].id
    ]
    assert len(ids(await chat_client.get("/api/documents", params={"locally_deleted": False}))) == 4
    # 필터를 겹치면 모두 만족하는 것만
    both = await chat_client.get("/api/documents", params={"path": r"C:\docs\보고서", "q": "암호"})
    assert ids(both) == [d["failed"].id]
    assert both.json()["counts"]["locally_deleted"] == 1  # 개수는 필터와 무관한 전체 기준

    # 페이지네이션
    p1 = await chat_client.get("/api/documents", params={"page_size": 2})
    p2 = await chat_client.get("/api/documents", params={"page_size": 2, "page": 2})
    p3 = await chat_client.get("/api/documents", params={"page_size": 2, "page": 3})
    p4 = await chat_client.get("/api/documents", params={"page_size": 2, "page": 4})
    assert ids(p1) == [d["ready"].id, d["failed"].id]
    assert ids(p2) == [d["indexed"].id, d["extracting"].id]
    assert ids(p3) == [d["embedding"].id]
    assert ids(p4) == []
    assert p3.json()["total"] == 5 and p3.json()["page_size"] == 2

    # 잘못된 값
    assert (await chat_client.get("/api/documents", params={"status": "nope"})).status_code == 422
    assert (await chat_client.get("/api/documents", params={"page": 0})).status_code == 422
    assert (await chat_client.get("/api/documents", params={"page_size": 1000})).status_code == 422


async def test_detail_and_folder_suggest(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    doc = await seed_doc(session, r"C:\Docs\프로젝트\알파\계획.md", ["첫째 문단", "둘째 문단"])
    await seed_doc(session, r"C:\Docs\프로젝트\베타\메모.txt", ["메모"])
    await seed_doc(session, r"C:\Docs\회의\회의록.txt", ["회의"])
    await seed_doc(session, r"C:\Docs\루트.md", ["루트"])

    with capture_logs() as logs:
        res = await chat_client.get(f"/api/documents/{doc.id}")
    assert res.status_code == 200
    body = res.json()
    assert body["filename"] == "계획.md" and body["status"] == "ready"
    assert [s["text"] for s in body["sections"]] == ["첫째 문단", "둘째 문단"]
    assert body["sections"][1]["location"] == {"lines": [3, 3]}
    assert body["sections"][0]["kind"] == "text"
    read = [r for r in logs if r["event"] == "documents.read"]
    assert read and read[0]["document_id"] == doc.id and "계획" not in str(read)
    for bad in ("999999999", "abc"):
        res = await chat_client.get(f"/api/documents/{bad}")
        assert res.status_code == 404 and res.json()["error"]["code"] == "document_not_found"

    async def suggest(prefix: str) -> list[str]:
        res = await chat_client.get("/api/documents/suggest", params={"prefix": prefix})
        assert res.status_code == 200
        return list(res.json())

    # 한 단계 아래 폴더까지, 원래 표기로, 대소문자 무시
    assert await suggest("c:\\docs\\") == [r"C:\Docs\프로젝트", r"C:\Docs\회의"]
    assert await suggest(r"C:\Docs\프") == [r"C:\Docs\프로젝트"]
    assert await suggest("C:\\Docs\\프로젝트\\") == [
        r"C:\Docs\프로젝트\베타",
        r"C:\Docs\프로젝트\알파",
    ]
    assert await suggest("C:\\Docs\\없는폴더") == []
    assert await suggest("") == [r"C:\Docs"]


async def test_reprocess_failed_sets_needs_upload(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    failed = await seed_doc(
        session,
        r"C:\docs\손상.docx",
        status=DocumentStatus.FAILED,
        failure_code=FailureCode.CORRUPTED,
        chunks=False,
    )
    no_text = await seed_doc(session, r"C:\docs\스캔.pdf", status=DocumentStatus.NO_TEXT)
    busy = await seed_doc(session, r"C:\docs\대기.md", ["대기"], status=DocumentStatus.QUEUED)
    meta = await seed_doc(session, r"C:\docs\큰파일.pdf", status=DocumentStatus.META_ONLY)

    with capture_logs() as logs:
        res = await chat_client.post(f"/api/documents/{failed.id}/reprocess")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["action"] == "needs_upload"
    assert body["document"]["needs_upload"] is True
    # 상태와 실패 사유는 그대로 두고(다시 올라올 때 새로 판정), 작업은 만들지 않는다
    assert body["document"]["status"] == "failed"
    assert body["document"]["failure_code"] == "corrupted"
    await session.refresh(failed)
    assert failed.needs_upload is True
    assert await count(session, Job, Job.document_id == failed.id) == 0
    events = [r for r in logs if r["event"] == "documents.reprocess.requested"]
    assert len(events) == 1
    assert events[0]["document_id"] == failed.id
    assert events[0]["action"] == "needs_upload"
    assert events[0]["previous_status"] == "failed"
    assert "손상" not in str(events)  # 경로·파일명은 남기지 않는다

    # 목록에서도 "다음 동기화 대기"(needs_upload)로 보인다
    listed = (await chat_client.get("/api/documents", params={"status": "failed"})).json()
    assert listed["items"][0]["needs_upload"] is True

    # 텍스트 없음도 원본을 다시 받아야 한다
    res = await chat_client.post(f"/api/documents/{no_text.id}/reprocess")
    assert res.status_code == 200 and res.json()["action"] == "needs_upload"

    # 대기·처리 중·파일명만은 재처리할 수 없다
    for doc in (busy, meta):
        res = await chat_client.post(f"/api/documents/{doc.id}/reprocess")
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "not_reprocessable"
    assert (await chat_client.post("/api/documents/999999999/reprocess")).status_code == 404

    # 다음 manifest 판정에서 해시가 같아도 업로드 대상이 된다
    from app.ingest.intake import FileMeta
    from app.sync.manifest import judge_manifest
    from app.sync.runs import start_run
    from app.sync.scope import SyncScope

    now = datetime.now(UTC)
    run = await start_run(session, SyncRunSource.CLI, now)
    scope = SyncScope(allowed_root="C:\\docs", roots=["C:\\docs"], excludes=[], max_file_mb=10)
    meta_item = FileMeta(path=failed.path, size=100, mtime=failed.mtime, sha256=failed.sha256)
    result = await judge_manifest(
        session, run, [meta_item], [], scope=scope, guard_ratio=1.0, now=now
    )
    assert result.upload == [failed.path]


async def test_reprocess_ready_rechunks_from_sections(
    chat_client: httpx.AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("UPLOAD_TMP_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    doc = await seed_doc(
        session, r"C:\docs\재청킹.md", ["첫째 문단은 예산 이야기다.", "둘째 문단은 일정 이야기다."]
    )
    # 예전 방식으로 만든 청크(섹션과 다름)와 기다리던 embed 작업
    await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    session.add(
        Chunk(
            document_id=doc.id,
            seq=0,
            location={},
            heading_path=[],
            filename=doc.filename,
            content="예전 청크",
            content_sha256="0" * 64,
            bm25_text="예전 청크",
            token_count=2,
        )
    )
    stale = await enqueue(session, type=JobType.EMBED, document=doc)
    await session.flush()

    with capture_logs() as logs:
        res = await chat_client.post(f"/api/documents/{doc.id}/reprocess")
    assert res.status_code == 200, res.text
    assert res.json()["action"] == "rechunk"
    assert res.json()["document"]["status"] == "extracting"  # 화면: 처리 중
    assert res.json()["document"]["needs_upload"] is False
    events = [r for r in logs if r["event"] == "documents.reprocess.requested"]
    assert events and events[0]["action"] == "rechunk" and events[0]["previous_status"] == "ready"
    jobs = (await session.scalars(select(Job).where(Job.document_id == doc.id))).all()
    await session.refresh(stale)
    assert stale.status == JobStatus.DONE  # 기다리던 작업은 재청킹이 대신한다
    queued = [j for j in jobs if j.status == JobStatus.QUEUED]
    assert [j.type for j in queued] == [JobType.INDEX]

    # Worker: 같은 연결 위의 세션 묶음으로 실제 작업 코드를 돌린다(업로드 없이 sections에서 재청킹)
    conn = await session.connection()
    sm = async_sessionmaker(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    runner = JobRunner(sm, FakeEmbeddingProvider(dim=EMBEDDER.dim))
    summary = await runner.run_until_idle()
    assert summary.counts.get("index.completed") == 1
    assert "extract.completed" not in summary.counts

    doc_id = doc.id
    session.expire_all()
    doc = await session.get_one(Document, doc_id)
    assert doc.status == DocumentStatus.READY
    chunks = (
        await session.scalars(select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.seq))
    ).all()
    joined = "\n".join(c.content for c in chunks)
    assert "예전 청크" not in joined
    assert "첫째 문단은 예산 이야기다." in joined and "둘째 문단은 일정 이야기다." in joined
    assert all(c.embedding is not None for c in chunks)
    assert await count(session, Section, Section.document_id == doc.id) == 2  # 섹션은 그대로


async def test_purge_removes_all_and_masks_citations(
    chat_client: httpx.AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("UPLOAD_TMP_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    docs = await seed_corpus(session)
    target = docs["project"]
    await enqueue(session, type=JobType.EMBED, document=target)
    original = original_path(target.id, target.sha256, target.ext)
    original.parent.mkdir(parents=True)
    original.write_text("임시 원본", encoding="utf-8")
    other_original = original_path(docs["contract"].id, "c" * 64, ".md")
    other_original.write_text("다른 문서", encoding="utf-8")

    # 두 문서를 인용한 답변
    res = await chat_client.post(
        "/api/conversations/new/messages", json={"text": "알파 프로젝트 마감일 알려줘"}
    )
    assert res.status_code == 200
    done = [d for n, d in parse_sse(res.text) if n == "done"][0]
    conv_id = done["conversation_id"]
    before = (await chat_client.get(f"/api/conversations/{conv_id}")).json()["messages"][1]
    cited = {c["document_id"] for c in before["citations"]}
    assert target.id in cited
    target_snippet = next(c for c in before["citations"] if c["document_id"] == target.id)
    assert target_snippet["snippet"] and target_snippet["snippet"] != DELETED_SNIPPET

    with capture_logs() as logs:
        res = await chat_client.delete(f"/api/documents/{target.id}")
    assert res.status_code == 204

    # documents·sections·chunks·jobs·임시 원본이 모두 사라진다(다른 문서는 그대로)
    assert await count(session, Document, Document.id == target.id) == 0
    assert await count(session, Section, Section.document_id == target.id) == 0
    assert await count(session, Chunk, Chunk.document_id == target.id) == 0
    assert await count(session, Job, Job.document_id == target.id) == 0
    assert not original.exists()
    assert other_original.exists()
    assert await count(session, Chunk, Chunk.document_id == docs["contract"].id) == 2
    assert (await chat_client.get(f"/api/documents/{target.id}")).status_code == 404
    assert target.id not in ids(await chat_client.get("/api/documents"))

    # 검색에서도 사라진다
    result = await hybrid_search(session, "알파 프로젝트 마감일", query_vector=None)
    assert target.id not in {h.document_id for h in result.hits}

    # 예전 대화의 인용 구절은 "삭제된 문서"로 가린다(경로·번호는 둠, 다른 문서 인용은 그대로)
    after = (await chat_client.get(f"/api/conversations/{conv_id}")).json()["messages"][1]
    for c_before, c_after in zip(before["citations"], after["citations"], strict=True):
        assert c_after["n"] == c_before["n"] and c_after["path"] == c_before["path"]
        if c_before["document_id"] == target.id:
            assert c_after["snippet"] == DELETED_SNIPPET and c_after["deleted"] is True
        else:
            assert c_after["snippet"] == c_before["snippet"] and "deleted" not in c_after
    assert target_snippet["snippet"] not in str(after["citations"])

    purged = [r for r in logs if r["event"] == "documents.purged"]
    assert len(purged) == 1
    assert purged[0]["reason"] == "user"
    assert purged[0]["documents"] == 1
    assert purged[0]["sections"] == 2 and purged[0]["chunks"] == 2 and purged[0]["jobs"] == 1
    assert purged[0]["masked_messages"] == 1
    assert "알파" not in str(purged)  # 원인과 개수만

    # 없는 문서는 404
    assert (await chat_client.delete(f"/api/documents/{target.id}")).status_code == 404

    # 30일 경과 자동 삭제(T14가 씀)는 인용을 그대로 둔다
    contract = docs["contract"]
    msg = await session.scalar(select(Message).where(Message.role == "assistant"))
    assert msg is not None
    msg.citations = [*msg.citations, {"n": 9, "document_id": contract.id, "snippet": "위약금"}]
    await session.flush()
    kept = await purge_documents(session, [contract.id], reason="retention")
    assert kept.documents == 1 and kept.masked_messages == 0
    await session.refresh(msg)
    assert msg.citations[-1]["snippet"] == "위약금"
