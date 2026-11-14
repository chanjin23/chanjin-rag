"""동기화 범위 설정 API (F-01, PRD 3장 수집 범위·서버 데이터 삭제, TRD 6.5, 8.3).

경로는 운영과 같은 Windows 경로 문자열(허용 폴더 `C:\\Chanjin\\dev`, 마이그레이션의 루트
초기값)이다. 서버는 경로를 문자열로만 비교하므로 실제 폴더는 없어도 된다.
문서는 합성 데이터(`chat_seed.seed_doc`).
"""

from typing import Any

import httpx
from chat_seed import seed_doc
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.db.enums import MessageRole, MessageStatus
from app.db.models import Chunk, Conversation, Document, Message, SyncConfig
from app.ingest.purge import DELETED_SNIPPET

ROOT = "C:\\Chanjin\\dev"
DEFAULT_EXCLUDES = [".git/", "node_modules/", ".venv/", "venv/", "__pycache__/", "~$*", "*.tmp"]


def p(rel: str) -> str:
    return f"{ROOT}\\{rel}"


async def count(session: AsyncSession, model: Any, *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def test_root_must_be_under_dev(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    res = await chat_client.get("/api/settings/sync")
    assert res.status_code == 200
    body = res.json()
    assert body["allowed_root"] == ROOT
    assert body["roots"] == [ROOT]

    bad = {
        "C:\\Users\\me\\Documents": "outside_root",
        "C:\\Chanjin\\devil": "outside_root",  # 이름이 같은 앞부분이어도 하위가 아님
        "C:\\Chanjin\\dev\\..\\secret": "outside_root",
        "D:\\Chanjin\\dev\\notes": "outside_root",
        "notes\\2026": "relative_path",
        "C:\\Chanjin\\dev\\project\\app": "fixed_exclude",
    }
    for root, code in bad.items():
        for method in ("preview", "save"):
            payload = {"roots": [p("notes"), root], "excludes": DEFAULT_EXCLUDES}
            if method == "preview":
                res = await chat_client.post("/api/settings/sync/preview", json=payload)
            else:
                res = await chat_client.put("/api/settings/sync", json=payload)
            assert res.status_code == 400, (root, method, res.text)
            assert res.json()["error"]["code"] == code, root

    # 거부된 요청은 아무것도 바꾸지 않는다
    config = await session.get(SyncConfig, 1)
    assert config is not None and config.roots == [ROOT]

    # 허용 폴더 자신과 하위는 받는다(끝 구분자·공백 정리, 같은 경로는 하나만)
    res = await chat_client.put(
        "/api/settings/sync",
        json={
            "roots": [f" {ROOT}\\notes\\ ", p("NOTES"), "C:/Chanjin/dev/메모"],
            "excludes": DEFAULT_EXCLUDES,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["roots"] == [p("notes"), "C:/Chanjin/dev/메모"]
    await session.refresh(config)
    assert config.roots == [p("notes"), "C:/Chanjin/dev/메모"]


async def test_preview_counts_without_saving(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    keep = await seed_doc(session, p("notes\\메모.md"), ["남길 메모"])
    for i in range(12):
        await seed_doc(session, p(f"archive\\옛자료{i:02d}.md"), [f"옛 자료 {i}"])
    tmp = await seed_doc(session, p("notes\\draft.tmp.md"), ["임시"])
    # 이미 범위 밖(허용 폴더 밖)인 문서는 범위에서 "빠지는" 문서가 아니다
    outside = await seed_doc(session, "C:\\other\\밖.md", ["밖"])

    payload = {"roots": [p("notes")], "excludes": [*DEFAULT_EXCLUDES, "draft.*"]}
    with capture_logs() as logs:
        res = await chat_client.post("/api/settings/sync/preview", json=payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 13  # archive 12개 + 새 제외 패턴에 걸린 1개
    assert len(body["examples"]) == 10
    assert all(e.startswith(p("archive")) for e in body["examples"])
    assert body["examples"] == sorted(body["examples"], key=str.lower)
    assert body["roots"] == [p("notes")]
    assert body["excludes"] == [*DEFAULT_EXCLUDES, "draft.*"]

    # 저장하지 않는다: 설정도 문서도 그대로
    config = await session.get(SyncConfig, 1)
    assert config is not None and config.roots == [ROOT] and config.excludes == DEFAULT_EXCLUDES
    assert await count(session, Document) == 15
    for doc in (keep, tmp, outside):
        assert await session.get(Document, doc.id) is not None

    previewed = [r for r in logs if r["event"] == "settings.sync.previewed"]
    assert len(previewed) == 1
    assert previewed[0]["count"] == 13
    assert previewed[0]["roots"] == 1 and previewed[0]["excludes"] == len(DEFAULT_EXCLUDES) + 1
    assert "archive" not in repr(previewed) and "메모" not in repr(previewed)
    assert not [r for r in logs if r["event"] == "documents.purged"]

    # 바꾸지 않으면 0개
    same = await chat_client.post(
        "/api/settings/sync/preview", json={"roots": [ROOT], "excludes": DEFAULT_EXCLUDES}
    )
    assert same.json()["count"] == 0 and same.json()["examples"] == []


async def test_save_purges_out_of_scope_and_masks_citations(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    keep = await seed_doc(session, p("notes\\회의록.md"), ["주간 회의는 월요일이다."])
    gone = await seed_doc(session, p("고객사\\계약.md"), ["위약금은 계약 금액의 10%다."])
    excluded = await seed_doc(session, p("notes\\비밀\\메모.md"), ["비밀 메모"])
    outside = await seed_doc(session, "C:\\other\\밖.md", ["밖"])

    conv = Conversation(title="t")
    session.add(conv)
    await session.flush()
    citations = [
        {"n": 1, "document_id": keep.id, "path": keep.path, "snippet": "주간 회의는 월요일."},
        {"n": 2, "document_id": gone.id, "path": gone.path, "snippet": "위약금은 10%다."},
    ]
    msg = Message(
        conversation_id=conv.id,
        role=MessageRole.ASSISTANT,
        content="답 [1][2]",
        citations=citations,
        status=MessageStatus.COMPLETE,
    )
    session.add(msg)
    await session.flush()

    new = {"roots": [p("notes")], "excludes": [*DEFAULT_EXCLUDES, "비밀/"]}
    preview = (await chat_client.post("/api/settings/sync/preview", json=new)).json()
    assert preview["count"] == 2

    # 미리보기와 수가 다르면 저장하지 않는다(확인받지 않은 수만큼 지우지 않음)
    stale = await chat_client.put("/api/settings/sync", json={**new, "expected_purge": 1})
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "preview_outdated",
        "message": stale.json()["error"]["message"],
        "count": 2,
    }
    assert await count(session, Document, Document.id == gone.id) == 1

    with capture_logs() as logs:
        res = await chat_client.put("/api/settings/sync", json={**new, "expected_purge": 2})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["purged"] == 2
    assert body["roots"] == [p("notes")]
    assert [e["pattern"] for e in body["excludes"] if not e["fixed"]] == new["excludes"]

    # 범위에서 빠진 문서는 영구 삭제(청크까지), 남은 범위·이미 범위 밖인 문서는 그대로
    assert await count(session, Document, Document.id.in_([gone.id, excluded.id])) == 0
    assert await count(session, Chunk, Chunk.document_id.in_([gone.id, excluded.id])) == 0
    assert await count(session, Document, Document.id.in_([keep.id, outside.id])) == 2
    config = await session.get(SyncConfig, 1)
    assert config is not None
    await session.refresh(config)
    assert config.roots == [p("notes")] and config.excludes == new["excludes"]

    # 예전 대화의 인용 구절은 "삭제된 문서"로 가린다(번호·경로는 둠)
    await session.refresh(msg)
    assert msg.citations[0] == citations[0]
    assert msg.citations[1]["snippet"] == DELETED_SNIPPET and msg.citations[1]["deleted"] is True
    assert msg.citations[1]["n"] == 2 and msg.citations[1]["path"] == gone.path

    purged = [r for r in logs if r["event"] == "documents.purged"]
    assert len(purged) == 1
    assert purged[0]["reason"] == "scope" and purged[0]["documents"] == 2
    assert purged[0]["masked_messages"] == 1
    updated = [r for r in logs if r["event"] == "settings.sync.updated"]
    assert len(updated) == 1
    assert updated[0]["purged"] == 2 and updated[0]["roots_removed"] == 1
    assert updated[0]["excludes_added"] == 1
    assert "고객사" not in repr(logs) and "비밀" not in repr(logs)

    # GET에도 반영된다
    got = (await chat_client.get("/api/settings/sync")).json()
    assert got["roots"] == [p("notes")]


async def test_fixed_excludes_not_editable(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    body = (await chat_client.get("/api/settings/sync")).json()
    fixed = [e["pattern"] for e in body["excludes"] if e["fixed"]]
    editable = [e["pattern"] for e in body["excludes"] if not e["fixed"]]
    assert fixed == ["/project/", "/backup/"]
    assert body["excludes"][:2] == [
        {"pattern": "/project/", "fixed": True},
        {"pattern": "/backup/", "fixed": True},
    ]
    assert editable == DEFAULT_EXCLUDES

    # 편집 목록에서 고정 제외를 빼고 저장해도 고정 제외는 그대로 적용된다
    res = await chat_client.put("/api/settings/sync", json={"roots": [ROOT], "excludes": []})
    assert res.status_code == 200
    after = res.json()["excludes"]
    assert after == [
        {"pattern": "/project/", "fixed": True},
        {"pattern": "/backup/", "fixed": True},
    ]

    # 고정 제외를 편집 목록에 넣어도 편집 가능한 패턴으로 저장되지 않는다(대소문자·구분자 무시)
    res = await chat_client.put(
        "/api/settings/sync",
        json={"roots": [ROOT], "excludes": ["/project/", "\\BACKUP\\", "/backup", "*.bak"]},
    )
    assert res.status_code == 200
    assert res.json()["excludes"] == [
        {"pattern": "/project/", "fixed": True},
        {"pattern": "/backup/", "fixed": True},
        {"pattern": "*.bak", "fixed": False},
    ]
    config = await session.get(SyncConfig, 1)
    assert config is not None
    await session.refresh(config)
    assert config.excludes == ["*.bak"]

    # 고정 제외 아래 폴더는 루트로 지정할 수 없다(범위에서 다시 넣는 방법 없음)
    for root in (p("project"), p("backup\\2026")):
        res = await chat_client.put("/api/settings/sync", json={"roots": [root], "excludes": []})
        assert res.status_code == 400
        assert res.json()["error"]["code"] == "fixed_exclude"

    # Agent 설정에도 고정 제외가 늘 앞에 들어 있다
    from app.api.sync import get_config

    agent_config = await get_config(session)
    assert [e.pattern for e in agent_config.excludes if e.fixed] == ["/project/", "/backup/"]
    assert [e.pattern for e in agent_config.excludes if not e.fixed] == ["*.bak"]
