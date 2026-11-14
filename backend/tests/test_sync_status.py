"""동기화 상태·보류된 삭제 적용 API (F-02, F-03, TRD 6.1 대량 삭제 보호, 8.3, 12.4).

실행 기록은 Sync API(Agent Bearer 토큰)로 만들고, 상태는 웹 API(세션)로 읽는다.
경로는 운영과 같은 Windows 경로 문자열이고 문서는 합성 데이터다.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from chat_seed import seed_doc
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.db.enums import JobStatus, JobType, SyncRunSource, SyncRunStatus
from app.db.keys import make_path_key
from app.db.models import Document, EmbedQuota, Job, SyncConfig, SyncRun
from app.jobs.quota import quota_day
from app.jobs.runner import utc_now

ROOT = "C:\\Chanjin\\dev"


def p(rel: str) -> str:
    return f"{ROOT}\\{rel}"


async def web_get(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    res = await client.get(url)
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


async def agent_run(client: httpx.AsyncClient, token: str, files: list[str]) -> dict[str, Any]:
    """Agent처럼 실행 시작 → manifest(`files`의 문서만 있음) → 완료."""
    headers = {"Authorization": f"Bearer {token}"}
    run = (await client.post("/api/sync/runs", json={"source": "cli"}, headers=headers)).json()
    entries = [
        {"path": f, "size": 100, "mtime": "2026-11-01T00:00:00+00:00", "sha256": "b" * 64}
        for f in files
    ]
    res = await client.post(
        f"/api/sync/runs/{run['run_id']}/manifest",
        json={"files": entries, "scan_errors": []},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    done = await client.post(
        f"/api/sync/runs/{run['run_id']}/complete", json={"status": "succeeded"}, headers=headers
    )
    assert done.status_code == 200
    body: dict[str, Any] = res.json()
    return body


async def test_status_fields(
    chat_client: httpx.AsyncClient,
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 백업 폴더는 빈 임시 폴더(개발 중 만든 var/backups의 백업이 섞이지 않게)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    get_settings.cache_clear()
    # 아무 기록도 없으면: 실행·백업 없음 → 하루 넘음 경고
    empty = await web_get(chat_client, "/api/sync/status")
    assert empty["running"] is None and empty["latest_run"] is None
    assert empty["last_success_at"] is None and empty["last_backup_at"] is None
    assert empty["held_deletions"] is None and empty["skipped_formats"] == {}
    assert empty["stale"] == {"sync": True, "backup": True}
    assert empty["embed"] == {"used": 0, "limit": 100}

    now = utc_now()
    old = SyncRun(
        source=SyncRunSource.SCHEDULE,
        started_at=now - timedelta(days=3),
        heartbeat_at=now - timedelta(days=3),
        finished_at=now - timedelta(days=3) + timedelta(minutes=2),
        status=SyncRunStatus.SUCCEEDED,
        stats={"skipped_formats": {".doc": 9}},
    )
    session.add(old)
    await session.flush()
    stale = await web_get(chat_client, "/api/sync/status")
    assert stale["stale"]["sync"] is True  # 마지막 성공이 하루 넘음
    assert stale["latest_run"]["id"] == old.id

    success = SyncRun(
        source=SyncRunSource.CLI,
        started_at=now - timedelta(hours=2),
        heartbeat_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2) + timedelta(minutes=1),
        status=SyncRunStatus.SUCCEEDED,
        stats={
            "manifest": {"files": 10, "upload": 2, "deleted": 0, "unchanged": 8},
            "uploaded": 2,
            "skipped_formats": {".doc": 3, ".xls": 1},
            "deletion_held": {},
        },
    )
    running = SyncRun(
        source=SyncRunSource.WEB,
        started_at=now - timedelta(minutes=1),
        heartbeat_at=now - timedelta(seconds=10),
        status=SyncRunStatus.RUNNING,
    )
    session.add_all([success, running])
    doc = await seed_doc(session, p("메모.md"), ["메모"])
    for job_type, status in [
        (JobType.EXTRACT, JobStatus.QUEUED),
        (JobType.EMBED, JobStatus.QUEUED),
        (JobType.EMBED, JobStatus.RUNNING),
        (JobType.INDEX, JobStatus.DONE),
    ]:
        session.add(
            Job(type=job_type, document_id=doc.id, sha256=doc.sha256, status=status, priority=now)
        )
    session.add(EmbedQuota(day=quota_day(now), used=37))
    await session.flush()

    body = await web_get(chat_client, "/api/sync/status")
    assert body["running"]["id"] == running.id
    assert body["running"]["status"] == "running" and body["running"]["source"] == "web"
    assert body["latest_run"]["id"] == running.id
    assert datetime.fromisoformat(body["last_success_at"]) == success.finished_at
    assert body["queue"] == {
        "queued": 2,
        "running": 1,
        "by_type": {"extract": 1, "index": 0, "embed": 2},
    }
    assert body["embed"] == {"used": 37, "limit": 100}
    assert body["skipped_formats"] == {".doc": 3, ".xls": 1}  # 마지막 성공 실행 기준
    assert body["stale"] == {"sync": False, "backup": True}  # 백업 파일 없음
    assert body["held_deletions"] is None
    assert datetime.fromisoformat(body["now"]).tzinfo is not None

    # 진행 중이던 실행이 끝나면 진행 상태가 사라진다
    await session.execute(
        update(SyncRun).where(SyncRun.id == running.id).values(status=SyncRunStatus.FAILED)
    )
    await session.flush()
    session.expire_all()
    body = await web_get(chat_client, "/api/sync/status")
    assert body["running"] is None
    assert body["latest_run"]["status"] == "failed"

    # 세션이 없으면 401
    from app.api.deps import require_web_user
    from app.main import app

    override = app.dependency_overrides.pop(require_web_user)
    try:
        assert (await chat_client.get("/api/sync/status")).status_code == 401
    finally:
        app.dependency_overrides[require_web_user] = override


async def test_apply_held_deletions(chat_client: httpx.AsyncClient, session: AsyncSession) -> None:
    from app.core import auth

    _, token = await auth.create_api_token(session, "pytest-agent")
    paths = [p(f"docs\\문서{i:02d}.md") for i in range(10)]
    for path in paths:
        doc = await seed_doc(session, path, [path])
        doc.path_key = make_path_key(path)  # manifest 판정은 정규화한 경로 키로 찾는다
    await session.flush()

    # 보류가 없으면 적용할 것이 없다
    res = await chat_client.post("/api/sync/held-deletions/apply", json={})
    assert res.status_code == 409 and res.json()["error"]["code"] == "no_held_deletions"

    # 10개 중 4개가 사라짐 → 대량 삭제 보호로 보류
    first = await agent_run(chat_client, token, paths[4:])
    assert sorted(first["deletion_held"]) == sorted(paths[:4]) and first["deleted"] == []

    status = await web_get(chat_client, "/api/sync/status")
    held = status["held_deletions"]
    assert held["run_id"] == first["run_id"]
    assert held["count"] == 4 and held["total"] == 10
    assert held["ratio"] == 0.4 and held["threshold"] == 0.2
    assert sorted(held["sample"]) == sorted(paths[:4])
    assert held["approved_at"] is None

    # 다른 실행 ID로 승인하면 거부(화면이 오래됨)
    res = await chat_client.post("/api/sync/held-deletions/apply", json={"run_id": 999999})
    assert res.status_code == 409 and res.json()["error"]["code"] == "held_deletions_outdated"

    with capture_logs() as logs:
        res = await chat_client.post(
            "/api/sync/held-deletions/apply", json={"run_id": first["run_id"]}
        )
    assert res.status_code == 200, res.text
    assert res.json()["run_id"] == first["run_id"] and res.json()["count"] == 4
    applied = [r for r in logs if r["event"] == "sync.held_deletions.applied"]
    assert len(applied) == 1
    assert applied[0]["run_id"] == first["run_id"] and applied[0]["count"] == 4
    assert "문서" not in repr(applied)

    # 승인만 하고 아직 삭제하지 않는다(다음 실행에서 적용)
    status = await web_get(chat_client, "/api/sync/status")
    assert status["held_deletions"]["approved_at"] is not None
    for path in paths:
        doc = await session.get(Document, (await _doc_id(session, path)))
        assert doc is not None and doc.locally_deleted is False

    # 다음 실행: 같은 4개가 여전히 없으면 보호 없이 로컬 삭제로 판정
    with capture_logs() as logs:
        second = await agent_run(chat_client, token, paths[4:])
    assert sorted(second["deleted"]) == sorted(paths[:4])
    assert second["deletion_held"] == []
    assert [r for r in logs if r["event"] == "sync.deletion.approval_used"]
    session.expire_all()
    for path in paths[:4]:
        doc = await session.get(Document, (await _doc_id(session, path)))
        assert doc is not None and doc.locally_deleted is True and doc.deleted_at is not None
    run = await session.get(SyncRun, second["run_id"])
    assert run is not None and run.stats["manifest"]["held_deletions_approved"] is True

    # 승인은 한 번만 쓴다: 경고가 사라지고, 다음 대량 삭제는 다시 보류된다
    config = await session.get(SyncConfig, 1)
    assert config is not None and config.held_deletions_approved_at is None
    status = await web_get(chat_client, "/api/sync/status")
    assert status["held_deletions"] is None
    third = await agent_run(chat_client, token, paths[4:6])
    assert third["deleted"] == [] and len(third["deletion_held"]) == 4


async def _doc_id(session: AsyncSession, path: str) -> int:
    doc_id = await session.scalar(select(Document.id).where(Document.path == path))
    assert doc_id is not None
    return int(doc_id)
