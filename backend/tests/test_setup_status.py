"""첫 실행 체크리스트 `/api/setup/status` (F-18, TRD 8.3)."""

from datetime import timedelta
from typing import Any

import httpx
from chat_seed import seed_doc
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.core import auth
from app.core.auth import utcnow
from app.db.enums import SyncRunSource, SyncRunStatus
from app.db.models import ApiToken, Document, SyncRun, User


async def _status(client: httpx.AsyncClient) -> dict[str, Any]:
    res = await client.get("/api/setup/status")
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


def _done(body: dict[str, Any]) -> dict[str, bool]:
    return {s["key"]: s["done"] for s in body["steps"]}


async def test_checklist_progress(chat_client: httpx.AsyncClient, session: AsyncSession) -> None:
    # 깨끗한 상태에서 시작(테스트 트랜잭션 안이라 끝나면 되돌아간다)
    for model in (Document, SyncRun, ApiToken, User):
        await session.execute(delete(model))
    await session.flush()

    with capture_logs() as logs:
        empty = await _status(chat_client)
    assert empty["user_exists"] is False and empty["token_issued"] is False
    assert empty["agent_first_seen_at"] is None and empty["first_sync_completed"] is False
    assert empty["documents"] == 0
    assert [s["key"] for s in empty["steps"]] == [
        "user",
        "token",
        "agent",
        "first_sync",
        "documents",
    ]
    assert all(not s["done"] for s in empty["steps"])
    commands = {s["key"]: s["command"] for s in empty["steps"]}
    assert "rag-backend create-token" in commands["token"]
    assert "rag-agent install" in commands["agent"]
    assert commands["first_sync"] == "rag-agent sync"
    assert empty["completed"] == 0 and empty["complete"] is False
    checked = [e for e in logs if e["event"] == "setup.status.checked"]
    assert checked and checked[0]["completed"] == 0 and checked[0]["documents"] == 0

    # 1) 사용자와 토큰
    session.add(User(username="admin", password_hash="x"))
    token, raw = await auth.create_api_token(session, "집 PC")
    await session.flush()
    step1 = await _status(chat_client)
    assert _done(step1) == {
        "user": True,
        "token": True,
        "agent": False,
        "first_sync": False,
        "documents": False,
    }
    assert step1["completed"] == 2

    # 폐기된 토큰만 있으면 발급 안 됨으로 본다
    await session.execute(update(ApiToken).values(revoked_at=utcnow()))
    await session.flush()
    assert (await _status(chat_client))["token_issued"] is False
    await session.execute(update(ApiToken).values(revoked_at=None))
    await session.flush()

    # 2) Agent 첫 접속(Bearer 토큰으로 Sync API 호출) → first_used_at 기록, 다시 불러도 그대로
    headers = {"Authorization": f"Bearer {raw}"}
    first = await chat_client.get("/api/sync/config", headers=headers)
    assert first.status_code == 200, first.text
    await session.refresh(token)
    first_seen = token.first_used_at
    assert first_seen is not None
    await chat_client.get("/api/sync/config", headers=headers)
    await session.refresh(token)
    assert token.first_used_at == first_seen
    step2 = await _status(chat_client)
    assert step2["agent_first_seen_at"] is not None
    assert _done(step2)["agent"] is True and _done(step2)["first_sync"] is False

    # 3) 실패한 실행은 첫 동기화로 치지 않고, 성공한 실행이 있으면 완료
    now = utcnow()
    session.add(
        SyncRun(
            source=SyncRunSource.CLI,
            started_at=now - timedelta(hours=2),
            finished_at=now - timedelta(hours=2),
            status=SyncRunStatus.FAILED,
        )
    )
    await session.flush()
    assert (await _status(chat_client))["first_sync_completed"] is False
    session.add(
        SyncRun(
            source=SyncRunSource.CLI,
            started_at=now - timedelta(hours=1),
            finished_at=now - timedelta(minutes=50),
            status=SyncRunStatus.SUCCEEDED,
        )
    )
    await session.flush()
    step3 = await _status(chat_client)
    assert step3["first_sync_completed"] is True and step3["first_sync_at"] is not None
    assert _done(step3)["documents"] is False

    # 4) 문서가 생기면 모든 단계 완료
    await seed_doc(session, r"C:\Chanjin\dev\메모.md", ["첫 문서"])
    await session.flush()
    with capture_logs() as logs:
        full = await _status(chat_client)
    assert full["documents"] == 1
    assert full["completed"] == full["total"] == 5 and full["complete"] is True
    checked = [e for e in logs if e["event"] == "setup.status.checked"]
    assert checked[0]["completed"] == 5 and checked[0]["pending"] == []
