"""manifest 판정 (TRD 6.1 판정표, 대량 삭제 보호, PRD 3장 동기화·서버 데이터 삭제).

경로는 운영과 같은 Windows 경로 문자열(허용 폴더 `C:\\Chanjin\\dev`)이다. 서버는 경로를 문자열로만
비교하므로 실제 폴더는 없어도 된다. 문서·청크는 테스트가 직접 만든 합성 데이터.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.db.enums import DocumentStatus, JobStatus, JobType, SectionKind
from app.db.models import EMBED_DIM, Chunk, Document, EmbedQuota, Job, Section, SyncRun
from app.ingest.chunking import filename_chunk
from app.jobs.quota import QuotaExhausted, quota_day
from app.jobs.runner import ClaimedJob, JobRunner
from app.providers.fake import FakeEmbeddingProvider

ROOT = "C:\\Chanjin\\dev"
MTIME = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def p(rel: str) -> str:
    return f"{ROOT}\\{rel}"


def entry(path: str, digest: str, *, size: int = 1000, mtime: datetime = MTIME) -> dict[str, Any]:
    return {"path": path, "size": size, "mtime": mtime.isoformat(), "sha256": digest}


def as_list(v: Any) -> list[float]:
    """halfvec 값(pgvector `HalfVector`)을 비교할 수 있는 목록으로."""
    return [float(x) for x in (v.to_list() if hasattr(v, "to_list") else v)]


def vec(seed: float) -> list[float]:
    return [seed] * EMBED_DIM


async def make_doc(
    session: AsyncSession,
    path: str,
    digest: str,
    *,
    status: DocumentStatus = DocumentStatus.READY,
    chunks: int = 2,
    embedded: bool = True,
    **fields: Any,
) -> Document:
    """처리가 끝난 문서(sections·chunks·임베딩 포함)를 만든다."""
    name = path.rsplit("\\", 1)[-1]
    doc = Document(
        path=path,
        path_key=path,
        filename=name,
        ext=name[name.rfind(".") :],
        size=1000,
        mtime=MTIME,
        sha256=digest,
        status=status,
        content_indexed=True,
        embed_charged_sha256=digest if embedded else None,
        embed_model="fake-embed" if embedded else None,
        embed_dim=EMBED_DIM if embedded else None,
        **fields,
    )
    session.add(doc)
    await session.flush()
    if status == DocumentStatus.META_ONLY:
        fc = filename_chunk(name, path)
        session.add(
            Chunk(
                document_id=doc.id,
                seq=0,
                location={},
                heading_path=[],
                filename=name,
                content=fc.content,
                content_sha256=fc.content_sha256,
                bm25_text=fc.bm25_text,
                token_count=fc.token_count,
            )
        )
    for i in range(chunks if status != DocumentStatus.META_ONLY else 0):
        session.add(
            Section(
                document_id=doc.id,
                seq=i,
                location={"page": i + 1},
                heading_path=["제목"],
                kind=SectionKind.TEXT,
                text=f"본문 {i} 매출 보고",
            )
        )
        session.add(
            Chunk(
                document_id=doc.id,
                seq=i,
                location={"page": i + 1},
                heading_path=["제목"],
                filename=name,
                content=f"본문 {i} 매출 보고",
                content_sha256=sha(f"chunk-{digest}-{i}"),
                bm25_text="본문 매출 보고",
                token_count=5,
                embedding=vec(0.01 * (i + 1)) if embedded else None,
                embed_model="fake-embed" if embedded else None,
            )
        )
    await session.flush()
    return doc


async def start_run(client: httpx.AsyncClient) -> int:
    res = await client.post("/api/sync/runs", json={"source": "cli"})
    assert res.status_code == 201, res.text
    return int(res.json()["run_id"])


async def post_manifest(
    client: httpx.AsyncClient, files: list[dict[str, Any]], scan_errors: list[str] | None = None
) -> dict[str, Any]:
    run_id = await start_run(client)
    res = await client.post(
        f"/api/sync/runs/{run_id}/manifest",
        json={"files": files, "scan_errors": scan_errors or []},
    )
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


async def count(session: AsyncSession, model: type[Any], *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


EMPTY = {
    "upload": [],
    "meta_only": [],
    "deleted": [],
    "restored": [],
    "moved": [],
    "copied": [],
    "deletion_held": [],
}


def lists(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in EMPTY}


# ---------------------------------------------------------------------------


async def test_new_file_upload(sync_client: httpx.AsyncClient, session: AsyncSession) -> None:
    path = p("notes\\회의록.md")
    with capture_logs() as logs:
        body = await post_manifest(sync_client, [entry(path, sha("new"))])

    assert lists(body) == {**EMPTY, "upload": [path]}
    # 판정만 하고 문서는 업로드가 올 때 만든다
    assert await count(session, Document) == 0
    run = await session.get(SyncRun, body["run_id"])
    assert run is not None
    assert run.stats["manifest"]["upload"] == 1
    assert run.stats["manifest"]["files"] == 1

    judged = [e for e in logs if e["event"] == "sync.manifest.judged"]
    assert len(judged) == 1
    assert judged[0]["upload"] == 1 and judged[0]["files"] == 1
    # 로그에는 건수만 남는다(경로·파일명 없음)
    assert "회의록" not in repr(logs)
    assert any(e["event"] == "sync.run.started" for e in logs)


async def test_same_hash_noop(sync_client: httpx.AsyncClient, session: AsyncSession) -> None:
    path = p("docs\\보고서.pdf")
    doc = await make_doc(session, path, sha("same"))
    newer = MTIME + timedelta(days=1)

    body = await post_manifest(sync_client, [entry(path, sha("same"), mtime=newer)])

    assert lists(body) == EMPTY
    assert body["counts"]["unchanged"] == 1
    await session.refresh(doc)
    assert doc.mtime == newer  # 수정 시각만 갱신
    assert doc.status == DocumentStatus.READY
    assert await count(session, Job) == 0


async def test_move_updates_path_only(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    old = p("old\\계약서.docx")
    new = p("archive\\2026\\계약서-최종.docx")
    doc = await make_doc(session, old, sha("contract"))
    big_old = p("old\\영상자료.pdf")
    big_new = p("archive\\영상자료.pdf")
    big = await make_doc(session, big_old, sha("big"), status=DocumentStatus.META_ONLY)
    vectors_before = [
        as_list(v)
        for v in (
            await session.scalars(
                select(Chunk.embedding).where(Chunk.document_id == doc.id).order_by(Chunk.seq)
            )
        ).all()
    ]

    body = await post_manifest(
        sync_client,
        [entry(new, sha("contract")), entry(big_new, sha("big"), size=20 * 1024 * 1024)],
    )

    assert lists(body) == {
        **EMPTY,
        "moved": [{"from": old, "to": new}, {"from": big_old, "to": big_new}],
    }
    await session.refresh(doc)
    assert doc.path == new
    assert doc.path_key == new.lower()
    assert doc.filename == "계약서-최종.docx"
    assert doc.status == DocumentStatus.READY
    assert not doc.locally_deleted
    assert await count(session, Document) == 2  # 새 문서를 만들지 않음

    chunks = (
        await session.scalars(select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.seq))
    ).all()
    assert [c.filename for c in chunks] == ["계약서-최종.docx"] * 2
    assert [as_list(c.embedding) for c in chunks] == vectors_before  # 임베딩 그대로
    assert await count(session, Section, Section.document_id == doc.id) == 2

    # 파일명 청크(meta_only)는 내용에 들어 있는 파일명·경로도 바뀐다
    meta_chunk = await session.scalar(select(Chunk).where(Chunk.document_id == big.id))
    assert meta_chunk is not None
    assert meta_chunk.content == f"영상자료.pdf\n{big_new}"
    assert meta_chunk.bm25_text == filename_chunk("영상자료.pdf", big_new).bm25_text

    # 업로드·작업·한도 사용 없음
    assert await count(session, Job) == 0
    assert await count(session, EmbedQuota) == 0


async def test_copy_duplicates_chunks_without_quota(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    original = p("docs\\제안서.pptx")
    copied = p("backup-copy\\제안서 - 복사본.pptx")
    src = await make_doc(session, original, sha("proposal"), chunks=3)

    body = await post_manifest(
        sync_client, [entry(original, sha("proposal")), entry(copied, sha("proposal"))]
    )

    assert lists(body) == {**EMPTY, "copied": [{"from": original, "to": copied}]}
    assert body["counts"]["unchanged"] == 1
    new = await session.scalar(select(Document).where(Document.path == copied))
    assert new is not None and new.id != src.id
    assert new.status == DocumentStatus.READY
    assert new.sha256 == sha("proposal")
    assert new.filename == "제안서 - 복사본.pptx"
    assert new.embed_charged_sha256 == sha("proposal")  # 한도를 다시 쓰지 않음

    src_chunks = (
        await session.scalars(select(Chunk).where(Chunk.document_id == src.id).order_by(Chunk.seq))
    ).all()
    new_chunks = (
        await session.scalars(select(Chunk).where(Chunk.document_id == new.id).order_by(Chunk.seq))
    ).all()
    assert len(new_chunks) == len(src_chunks) == 3
    assert [c.content for c in new_chunks] == [c.content for c in src_chunks]
    assert [as_list(c.embedding) for c in new_chunks] == [as_list(c.embedding) for c in src_chunks]
    assert {c.filename for c in new_chunks} == {"제안서 - 복사본.pptx"}
    assert await count(session, Section, Section.document_id == new.id) == 3
    # 원본은 그대로
    assert await count(session, Chunk, Chunk.document_id == src.id) == 3
    # 업로드·작업·한도 사용 없음
    assert await count(session, Job) == 0
    assert await count(session, EmbedQuota) == 0


async def test_missing_marks_locally_deleted(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    paths = [p(f"memo\\메모{i}.txt") for i in range(5)]
    docs = [await make_doc(session, path, sha(path)) for path in paths]
    # 범위 밖(고정 제외 아래) 문서는 삭제 판정 대상이 아니다
    outside = await make_doc(session, p("project\\app\\README.md"), sha("outside"))

    # 5개 중 1개(20%)는 보호 기준(20% 초과)에 걸리지 않는다
    body = await post_manifest(sync_client, [entry(x, sha(x)) for x in paths[1:]])

    assert lists(body) == {**EMPTY, "deleted": [paths[0]]}
    for d in [*docs, outside]:
        await session.refresh(d)
    assert docs[0].locally_deleted is True
    assert docs[0].deleted_at is not None
    assert all(not d.locally_deleted for d in docs[1:])
    assert outside.locally_deleted is False
    # 로컬 삭제는 표시만 하고 데이터는 남긴다(30일간 검색)
    assert await count(session, Chunk, Chunk.document_id == docs[0].id) == 2


async def test_scan_error_holds_deletion(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    keep = [p("a\\하나.md"), p("a\\둘.md"), p("a\\셋.md"), p("a\\넷.md")]
    unreadable = [p("locked\\비밀.md"), p("locked\\sub\\메모.md")]
    locked_file = p("a\\열린파일.docx")  # 다른 프로그램이 잠근 파일은 파일 경로로 온다
    for path in [*keep, *unreadable, locked_file]:
        await make_doc(session, path, sha(path))

    with capture_logs() as logs:
        body = await post_manifest(
            sync_client,
            [entry(x, sha(x)) for x in keep],
            scan_errors=[p("LOCKED"), locked_file],
        )

    assert lists(body) == EMPTY
    assert sorted(body["scan_error_held"]) == sorted([*unreadable, locked_file])
    flagged = await count(session, Document, Document.locally_deleted.is_(True))
    assert flagged == 0
    run = await session.get(SyncRun, body["run_id"])
    assert run is not None
    assert run.stats["deletion_held"]["scan_error"]["count"] == 3
    held = [e for e in logs if e["event"] == "sync.deletion.held"]
    assert [(e["reason"], e["count"]) for e in held] == [("scan_error", 3)]


async def test_mass_delete_guard_holds_all(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    paths = [p(f"docs\\문서{i:02d}.md") for i in range(10)]
    for path in paths:
        await make_doc(session, path, sha(path))

    # 10개 중 3개(30%)가 사라짐 → 20% 초과라 모두 보류
    with capture_logs() as logs:
        body = await post_manifest(sync_client, [entry(x, sha(x)) for x in paths[3:]])

    assert body["deleted"] == []
    assert sorted(body["deletion_held"]) == sorted(paths[:3])
    assert await count(session, Document, Document.locally_deleted.is_(True)) == 0
    run = await session.get(SyncRun, body["run_id"])
    assert run is not None
    guard = run.stats["deletion_held"]["mass_delete"]
    assert guard["count"] == 3 and guard["total"] == 10
    assert guard["ratio"] == 0.3 and guard["threshold"] == 0.2
    assert sorted(guard["sample"]) == sorted(paths[:3])
    held = [e for e in logs if e["event"] == "sync.deletion.held"]
    assert len(held) == 1
    assert held[0]["reason"] == "mass_delete_guard" and held[0]["count"] == 3
    assert "문서" not in repr(held)


async def test_restored_clears_flag(sync_client: httpx.AsyncClient, session: AsyncSession) -> None:
    path = p("notes\\돌아온 파일.md")
    doc = await make_doc(
        session,
        path,
        sha("back"),
        locally_deleted=True,
        deleted_at=MTIME,
    )

    body = await post_manifest(sync_client, [entry(path, sha("back"))])

    assert lists(body) == {**EMPTY, "restored": [path]}
    await session.refresh(doc)
    assert doc.locally_deleted is False
    assert doc.deleted_at is None
    assert await count(session, Job) == 0


async def test_over_limit_is_meta_only(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    big = p("media\\설계도면.pdf")
    small = p("media\\요약.pdf")
    eleven_mb = 11 * 1024 * 1024
    body = await post_manifest(
        sync_client, [entry(big, sha("big"), size=eleven_mb), entry(small, sha("small"))]
    )
    assert lists(body) == {**EMPTY, "upload": [small], "meta_only": [big]}

    with capture_logs() as logs:
        res = await sync_client.post(
            "/api/sync/files/meta",
            json={
                "files": [
                    entry(big, sha("big"), size=eleven_mb),
                    entry(small, sha("small"), size=1000),  # 10MB 이하는 업로드해야 함
                ]
            },
        )
    assert res.status_code == 200, res.text
    out = res.json()
    assert [a["outcome"] for a in out["accepted"]] == ["meta_only"]
    assert out["rejected"] == [{"path": small, "reason": "not_over_limit"}]

    doc = await session.get(Document, out["accepted"][0]["document_id"])
    assert doc is not None
    assert doc.status == DocumentStatus.META_ONLY
    assert doc.size == eleven_mb
    chunks = (await session.scalars(select(Chunk).where(Chunk.document_id == doc.id))).all()
    assert [c.content for c in chunks] == [f"설계도면.pdf\n{big}"]  # 파일명만 색인
    assert await count(session, Job) == 0
    assert any(e["event"] == "sync.file.meta_registered" for e in logs)

    # 다음 실행에서는 같은 해시라 아무것도 하지 않는다
    again = await post_manifest(sync_client, [entry(big, sha("big"), size=eleven_mb)])
    assert lists(again) == {**EMPTY}


async def test_needs_upload_requested(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    path = p("docs\\깨진파일.docx")
    await make_doc(
        session,
        path,
        sha("broken"),
        status=DocumentStatus.FAILED,
        needs_upload=True,
        embedded=False,
    )
    ok = p("docs\\정상.docx")
    await make_doc(session, ok, sha("ok"))

    body = await post_manifest(sync_client, [entry(path, sha("broken")), entry(ok, sha("ok"))])

    assert lists(body) == {**EMPTY, "upload": [path]}


async def test_changed_hash_uploads_and_rejects_out_of_scope(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    path = p("docs\\수정됨.md")
    await make_doc(session, path, sha("v1"))

    body = await post_manifest(
        sync_client,
        [
            entry(path, sha("v2")),
            entry(p("project\\x.md"), sha("x")),
            entry("D:\\other\\y.md", sha("y")),
            entry(p("docs\\z.exe"), sha("z")),
        ],
    )

    assert lists(body) == {**EMPTY, "upload": [path]}
    assert [r["reason"] for r in body["rejected"]] == [
        "fixed_exclude",
        "outside_root",
        "unsupported_extension",
    ]


async def test_copy_falls_back_to_upload_while_source_in_progress(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    first = p("docs\\처리중.md")
    second = p("docs\\처리중 - 복사본.md")
    await make_doc(session, first, sha("busy"), status=DocumentStatus.QUEUED, embedded=False)

    body = await post_manifest(sync_client, [entry(first, sha("busy")), entry(second, sha("busy"))])

    assert lists(body) == {**EMPTY, "upload": [second]}


def same_connection_runner(session: AsyncSession, now: datetime) -> JobRunner:
    """테스트 세션과 같은 연결(같은 롤백 트랜잭션)을 쓰는 JobRunner. 고정 시계."""
    sm = async_sessionmaker(
        bind=session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    return JobRunner(sm, FakeEmbeddingProvider(EMBED_DIM), clock=lambda: now)


async def test_copy_of_unembedded_source_uses_daily_quota(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """원본이 아직 임베딩 전(indexed)이면 복사할 벡터가 없다. 복사본의 임베딩은 API를 실제로
    부르므로 다른 파일과 똑같이 하루 파일 한도를 쓴다(한도가 차면 가져가지 않음)."""
    original = p("docs\대기중.md")
    copied = p("docs\대기중 - 복사본.md")
    src = await make_doc(
        session, original, sha("pending"), status=DocumentStatus.INDEXED, embedded=False
    )

    body = await post_manifest(
        sync_client, [entry(original, sha("pending")), entry(copied, sha("pending"))]
    )

    assert lists(body) == {**EMPTY, "copied": [{"from": original, "to": copied}]}
    new = await session.scalar(select(Document).where(Document.path == copied))
    assert new is not None
    assert new.status == DocumentStatus.INDEXED
    assert new.embed_charged_sha256 is None  # 한도를 건너뛰는 표시를 하지 않는다
    assert await count(session, Chunk, Chunk.document_id == new.id) == 2  # 벡터 없는 청크 복사
    jobs = (await session.scalars(select(Job).where(Job.document_id == new.id))).all()
    assert [(j.type, j.status) for j in jobs] == [(JobType.EMBED, JobStatus.QUEUED)]
    assert await count(session, Job, Job.document_id == src.id) == 0

    # 오늘 파일 한도가 이미 찼으면 복사본의 embed 작업도 가져가지 않는다
    now = datetime.now(UTC) + timedelta(minutes=1)
    limit = get_settings().embed_daily_file_limit
    quota = EmbedQuota(day=quota_day(now), used=limit, chunks_used=0)
    session.add(quota)
    await session.flush()
    runner = same_connection_runner(session, now)
    claimed = await runner.claim_embed()
    assert isinstance(claimed, QuotaExhausted)
    assert claimed.reason == "files"
    await session.refresh(jobs[0])
    assert jobs[0].status == JobStatus.QUEUED

    # 한도가 남아 있으면 가져가면서 한도를 1 쓴다
    quota.used = limit - 1
    await session.flush()
    claimed = await runner.claim_embed()
    assert isinstance(claimed, ClaimedJob)
    assert claimed.document_id == new.id
    assert claimed.charged is True
    await session.refresh(quota)
    assert quota.used == limit


async def test_move_requires_same_extension(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """같은 해시라도 확장자가 바뀌면(.txt → .md) 파서가 달라지므로 이동이 아니라 업로드."""
    old = p("notes\메모.txt")
    new = p("notes\메모.md")
    doc = await make_doc(session, old, sha("memo"))
    others = [p(f"notes\기타{i}.md") for i in range(4)]
    for path in others:
        await make_doc(session, path, sha(path))

    body = await post_manifest(
        sync_client, [entry(new, sha("memo")), *[entry(x, sha(x)) for x in others]]
    )

    # 5개 중 1개(20%)라 보호에 걸리지 않고 옛 경로는 로컬 삭제
    assert lists(body) == {**EMPTY, "upload": [new], "deleted": [old]}
    await session.refresh(doc)
    assert doc.path == old and doc.ext == ".txt"
    assert doc.locally_deleted is True


async def test_scan_error_path_is_not_move_source(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """읽지 못한 폴더의 파일은 사라진 것이 아니므로 이동 원본으로 쓰지 않는다(복사 또는 업로드)."""
    ready_old = p("locked\보고서.md")
    busy_old = p("locked\처리중.md")
    ready_new = p("open\보고서.md")
    busy_new = p("open\처리중.md")
    ready = await make_doc(session, ready_old, sha("ready"))
    busy = await make_doc(
        session, busy_old, sha("busy"), status=DocumentStatus.QUEUED, embedded=False
    )

    body = await post_manifest(
        sync_client,
        [entry(ready_new, sha("ready")), entry(busy_new, sha("busy"))],
        scan_errors=[p("locked")],
    )

    assert lists(body) == {
        **EMPTY,
        "copied": [{"from": ready_old, "to": ready_new}],
        "upload": [busy_new],
    }
    assert sorted(body["scan_error_held"]) == sorted([ready_old, busy_old])
    for d in (ready, busy):
        await session.refresh(d)
        assert not d.locally_deleted
    assert (ready.path, busy.path) == (ready_old, busy_old)  # 옛 경로 그대로
