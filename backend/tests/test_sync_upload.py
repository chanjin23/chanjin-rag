"""Sync 업로드 `PUT /api/sync/files` (TRD 6.1, 8.2, 11장 업로드).

서버 해시 재계산(409 `hash_mismatch`), 경로 검증(루트 안·고정 제외 아님·확장자 화이트리스트),
임시 원본 저장 후 `extract` 작업 등록, Bearer 토큰 필수. 파일 내용은 테스트가 만든 합성 바이트.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.db.enums import DocumentStatus, JobType
from app.db.models import ApiToken, Document, Job, SyncConfig

ROOT = "C:\\Chanjin\\dev"
MTIME = datetime(2026, 11, 2, 10, 30, tzinfo=UTC)
CONTENT = "# 회의\n\n다음 배포는 금요일에 한다.\n".encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def put_file(
    client: httpx.AsyncClient,
    path: str,
    data: bytes = CONTENT,
    *,
    digest: str | None = None,
    headers: dict[str, str] | None = None,
    run_id: int | None = None,
) -> httpx.Response:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    form = {"path": path, "sha256": digest or sha(data), "mtime": MTIME.isoformat()}
    if run_id is not None:
        form["run_id"] = str(run_id)
    return await client.put(
        "/api/sync/files",
        data=form,
        files={"file": (name, data, "application/octet-stream")},
        headers=headers,
    )


def tmp_files() -> list[str]:
    folder = get_settings().upload_tmp_path
    return sorted(f.name for f in folder.iterdir()) if folder.is_dir() else []


async def count(session: AsyncSession, model: type[Any], *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def test_upload_queues_extract(sync_client: httpx.AsyncClient, session: AsyncSession) -> None:
    path = f"{ROOT}\\notes\\회의록.md"
    run = await sync_client.post("/api/sync/runs", json={"source": "cli"})
    run_id = run.json()["run_id"]
    with capture_logs() as logs:
        res = await put_file(sync_client, path, run_id=run_id)

    assert res.status_code == 200, res.text
    assert res.json()["outcome"] == "queued"
    doc = await session.get(Document, res.json()["document_id"])
    assert doc is not None
    assert (doc.path, doc.status, doc.sha256, doc.size) == (
        path,
        DocumentStatus.QUEUED,
        sha(CONTENT),
        len(CONTENT),
    )
    assert doc.mtime == MTIME
    jobs = (await session.scalars(select(Job).where(Job.document_id == doc.id))).all()
    assert [(j.type, j.sha256) for j in jobs] == [(JobType.EXTRACT, sha(CONTENT))]
    # 임시 원본은 버전별 이름으로 남고, 받는 중 파일(.part)은 남지 않는다
    assert tmp_files() == [f"{doc.id}-{sha(CONTENT)}.md"]
    stored = get_settings().upload_tmp_path / f"{doc.id}-{sha(CONTENT)}.md"
    assert stored.read_bytes() == CONTENT

    uploaded = [e for e in logs if e["event"] == "sync.file.uploaded"]
    assert len(uploaded) == 1
    assert uploaded[0]["document_id"] == doc.id and uploaded[0]["outcome"] == "queued"
    # 실행 단위 추적용 상관 키(경로·파일명 없음)
    assert uploaded[0]["run_id"] == run_id
    assert uploaded[0]["sha256_prefix"] == sha(CONTENT)[:8]
    assert "회의록" not in repr(logs)

    # 같은 내용을 다시 올리면 변경 없음(작업 추가 없음)
    again = await put_file(sync_client, path)
    assert again.json()["outcome"] == "unchanged"
    assert await count(session, Job) == 1
    assert tmp_files() == [f"{doc.id}-{sha(CONTENT)}.md"]


async def test_hash_mismatch_returns_409(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    path = f"{ROOT}\\notes\\바뀐파일.md"
    with capture_logs() as logs:
        res = await put_file(sync_client, path, digest=sha("manifest 때의 내용".encode()))

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "hash_mismatch"
    # 저장하지 않는다: 문서·작업·임시 파일 없음
    assert await count(session, Document) == 0
    assert await count(session, Job) == 0
    assert tmp_files() == []
    assert any(e["event"] == "sync.file.hash_mismatch" for e in logs)
    assert not any(e["event"] == "sync.file.uploaded" for e in logs)


async def test_path_outside_root_rejected(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    for path in [
        "D:\\other\\a.md",
        "C:\\Chanjin\\devil\\a.md",  # 이름이 허용 폴더로 시작할 뿐인 다른 폴더
        f"{ROOT}\\docs\\..\\..\\Windows\\a.md",
        "notes\\a.md",  # 상대 경로
    ]:
        res = await put_file(sync_client, path)
        assert res.status_code == 400, path
        assert res.json()["error"]["code"] in ("outside_root", "relative_path"), path

    # 허용 폴더 안이라도 sync_config의 루트 밖이면 거부
    await session.execute(update(SyncConfig).values(roots=[f"{ROOT}\\docs"]))
    res = await put_file(sync_client, f"{ROOT}\\other\\a.md")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "outside_root"
    ok = await put_file(sync_client, f"{ROOT}\\docs\\a.md")
    assert ok.status_code == 200, ok.text

    assert await count(session, Document) == 1
    assert tmp_files() == [f"{ok.json()['document_id']}-{sha(CONTENT)}.md"]


async def test_fixed_exclude_rejected(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    for path in [
        f"{ROOT}\\project\\chanjin-rag\\README.md",
        f"{ROOT}\\Backup\\rag-20261101.md",  # 대소문자 무시
    ]:
        res = await put_file(sync_client, path)
        assert res.status_code == 400, path
        assert res.json()["error"]["code"] == "fixed_exclude"

    # 기본 제외 패턴(웹에서 편집 가능)도 서버가 다시 검증
    res = await put_file(sync_client, f"{ROOT}\\docs\\node_modules\\pkg\\readme.md")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "excluded"

    # 하위의 같은 이름 폴더는 고정 제외가 아니다(앞의 `/` 기준)
    ok = await put_file(sync_client, f"{ROOT}\\docs\\project\\계획.md")
    assert ok.status_code == 200, ok.text
    assert await count(session, Document) == 1


async def test_extension_whitelist_case_insensitive(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    accepted = []
    for i, name in enumerate(["보고서.MD", "자료.Pdf", "표.XLSX", "메모.txt"]):
        res = await put_file(sync_client, f"{ROOT}\\docs\\{name}", CONTENT + bytes([i]))
        assert res.status_code == 200, (name, res.text)
        accepted.append(res.json()["document_id"])
    docs = (await session.scalars(select(Document).where(Document.id.in_(accepted)))).all()
    assert sorted(d.ext for d in docs) == [".md", ".pdf", ".txt", ".xlsx"]

    for name in ["실행파일.exe", "예전문서.doc", "확장자없음", "사진.png"]:
        res = await put_file(sync_client, f"{ROOT}\\docs\\{name}")
        assert res.status_code == 400, name
        assert res.json()["error"]["code"] == "unsupported_extension"
    assert await count(session, Document) == 4


async def test_file_over_limit_returns_413(
    sync_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await session.execute(update(SyncConfig).values(max_file_mb=1))
    data = b"x" * (1024 * 1024 + 1)
    res = await put_file(sync_client, f"{ROOT}\\docs\\큰파일.txt", data)
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "file_too_large"
    assert await count(session, Document) == 0
    assert tmp_files() == []


async def test_requires_bearer_token(
    api_client: httpx.AsyncClient, session: AsyncSession, tmp_path: Path
) -> None:
    calls = [
        ("GET", "/api/sync/config", None),
        ("POST", "/api/sync/runs", {"source": "cli"}),
        ("POST", "/api/sync/runs/1/heartbeat", None),
        ("POST", "/api/sync/runs/1/manifest", {"files": [], "scan_errors": []}),
        ("POST", "/api/sync/runs/1/complete", {"status": "succeeded"}),
        ("POST", "/api/sync/files/meta", {"files": []}),
    ]
    for method, url, body in calls:
        res = await api_client.request(method, url, json=body)
        assert res.status_code == 401, url
        assert res.json()["error"]["code"] == "invalid_token"
        assert res.headers["www-authenticate"] == "Bearer"
    res = await put_file(api_client, f"{ROOT}\\docs\\a.md")
    assert res.status_code == 401
    res = await put_file(api_client, f"{ROOT}\\docs\\a.md", headers={"Authorization": "Bearer x"})
    assert res.status_code == 401

    # 웹 세션 쿠키로는 쓸 수 없고, 폐기된 토큰도 거부된다
    from app.core import auth

    token, raw = await auth.create_api_token(session, "revoked")
    bearer = {"Authorization": f"Bearer {raw}"}
    ok = await api_client.get("/api/sync/config", headers=bearer)
    assert ok.status_code == 200
    await session.execute(
        update(ApiToken).where(ApiToken.id == token.id).values(revoked_at=datetime.now(UTC))
    )
    res = await put_file(api_client, f"{ROOT}\\docs\\a.md", headers=bearer)
    assert res.status_code == 401
    assert await count(session, Document) == 0


async def test_sync_config_lists_fixed_excludes(sync_client: httpx.AsyncClient) -> None:
    res = await sync_client.get("/api/sync/config")
    assert res.status_code == 200
    body = res.json()
    assert body["roots"] == [ROOT]
    assert body["allowed_root"] == ROOT
    assert body["max_file_mb"] == 10
    fixed = [e["pattern"] for e in body["excludes"] if e["fixed"]]
    editable = [e["pattern"] for e in body["excludes"] if not e["fixed"]]
    assert fixed == ["/project/", "/backup/"]
    assert "node_modules/" in editable and "~$*" in editable
    assert ".pptx" in body["extensions"] and ".hwp" in body["extensions"]
