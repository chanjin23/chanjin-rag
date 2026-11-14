"""첫 실행 체크리스트 `GET /setup/status` (F-18, TRD 7.1·8.3·9.1).

문서가 0개인 채팅 화면이 빈 화면 대신 이 체크리스트를 보여준다. 단계마다 할 명령과 완료 여부:

1. `user`: 관리자 계정 만들기 — `users`에 행이 있음
2. `token`: Agent 토큰 발급 — 폐기되지 않은 `api_tokens`가 있음
3. `agent`: Agent 설치·로그인 — 토큰으로 처음 접속한 시각(`api_tokens.first_used_at`)이 있음
4. `first_sync`: 첫 동기화 — `sync_runs`에 `succeeded` 실행이 있음
5. `documents`: 문서 수집 — 문서가 1개 이상

로그 `setup.status.checked`(완료한 단계 수, 문서 수). 비밀값·경로는 남기지 않는다.
"""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DbSession
from app.core.logging import get_logger
from app.db.enums import SyncRunStatus
from app.db.models import ApiToken, Document, SyncRun, User

router = APIRouter(tags=["setup"])
log = get_logger(__name__)


class SetupStep(BaseModel):
    key: str
    title: str
    command: str | None
    description: str
    done: bool
    done_at: datetime | None = None


class SetupStatus(BaseModel):
    user_exists: bool
    token_issued: bool
    agent_first_seen_at: datetime | None
    first_sync_completed: bool
    first_sync_at: datetime | None
    documents: int
    steps: list[SetupStep]
    completed: int
    total: int
    complete: bool


@router.get("/setup/status")
async def setup_status(db: DbSession) -> SetupStatus:
    # 표마다 한 번씩: 개수와 최초 시각을 함께 읽는다
    users = int(await db.scalar(select(func.count()).select_from(User)) or 0)
    active = ApiToken.revoked_at.is_(None)
    tokens, token_created_at, agent_first_seen = (
        await db.execute(
            select(
                func.count().filter(active),
                func.min(ApiToken.created_at).filter(active),
                func.min(ApiToken.first_used_at),
            )
        )
    ).one()
    tokens = int(tokens or 0)
    succeeded = SyncRun.status == SyncRunStatus.SUCCEEDED
    sync_count, first_sync_at = (
        await db.execute(select(func.count(), func.min(SyncRun.finished_at)).where(succeeded))
    ).one()
    first_sync_done = bool(sync_count)
    documents = int(await db.scalar(select(func.count()).select_from(Document)) or 0)

    steps = [
        SetupStep(
            key="user",
            title="관리자 계정 만들기",
            command="uv run rag-backend create-user <이름>",
            description="서버에서 웹 로그인 계정을 만듭니다",
            done=users > 0,
        ),
        SetupStep(
            key="token",
            title="Agent 토큰 발급",
            command="uv run rag-backend create-token <PC 이름>",
            description="서버에서 실행하면 토큰이 한 번만 표시됩니다",
            done=tokens > 0,
            done_at=token_created_at if tokens > 0 else None,
        ),
        SetupStep(
            key="agent",
            title="Sync Agent 설치와 로그인",
            command="rag-agent login --server <서버 주소>  →  rag-agent install",
            description="Windows PC에서 토큰을 저장하고 작업 스케줄러에 등록합니다",
            done=agent_first_seen is not None,
            done_at=agent_first_seen,
        ),
        SetupStep(
            key="first_sync",
            title="첫 동기화",
            command="rag-agent sync",
            description="문서 폴더를 서버로 올립니다. 이후에는 하루 4회 자동으로 실행됩니다",
            done=first_sync_done,
            done_at=first_sync_at,
        ),
        SetupStep(
            key="documents",
            title="문서 처리 확인",
            command=None,
            description="올린 문서가 수집되면 질문할 수 있습니다(문서 화면에서 상태 확인)",
            done=documents > 0,
        ),
    ]
    completed = sum(1 for s in steps if s.done)
    result = SetupStatus(
        user_exists=users > 0,
        token_issued=tokens > 0,
        agent_first_seen_at=agent_first_seen,
        first_sync_completed=first_sync_done,
        first_sync_at=first_sync_at,
        documents=documents,
        steps=steps,
        completed=completed,
        total=len(steps),
        complete=completed == len(steps),
    )
    log.info(
        "setup.status.checked",
        completed=completed,
        total=len(steps),
        pending=[s.key for s in steps if not s.done],
        documents=documents,
    )
    return result
