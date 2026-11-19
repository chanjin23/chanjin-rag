"""색인 범위 API (F-18, TRD 8.3). 채팅 입력창 위 한 줄에 쓴다.

`GET /index/coverage`:

- `by_status`: 상태별 문서 수
- `total_docs`, `bm25_docs`(키워드 검색 가능), `vector_docs`(의미 검색 가능),
  `pending_docs`(대기·처리 중): 질의의 검색 범위와 같은 기준(`app.query.search.compute_coverage`)
- `embed_waiting`: 아직 임베딩을 기다리는 파일 수(`queued`, `extracting`, `indexed`, `embedding`)
- `daily_limit`, `used_today`: 하루 임베딩 한도와 오늘 사용량(한국 시간 날짜)
- `estimated_completion`: 완료 예상일. 오늘 남은 한도를 먼저 쓰고, 나머지를 하루 한도로 나눈 날 수를
  더한다. 기다리는 파일이 없으면 null.
"""

import math
from datetime import date, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DbSession
from app.core.auth import utcnow
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import DocumentStatus
from app.db.models import Document, EmbedQuota
from app.jobs.quota import quota_day
from app.query.search import compute_coverage

router = APIRouter(tags=["index"])
log = get_logger(__name__)

EMBED_WAITING = (
    DocumentStatus.QUEUED,
    DocumentStatus.EXTRACTING,
    DocumentStatus.INDEXED,
    DocumentStatus.EMBEDDING,
)


class IndexCoverage(BaseModel):
    by_status: dict[str, int]
    total_docs: int
    bm25_docs: int
    vector_docs: int
    pending_docs: int
    embed_waiting: int
    daily_limit: int
    used_today: int
    estimated_completion: date | None


def estimate_completion(waiting: int, *, today: date, limit: int, used_today: int) -> date | None:
    """대기 파일 수 ÷ 하루 한도(오늘 남은 한도를 먼저 쓴다)."""
    if waiting <= 0:
        return None
    if limit <= 0:
        return None
    remaining_today = max(0, limit - used_today)
    if waiting <= remaining_today:
        return today
    return today + timedelta(days=math.ceil((waiting - remaining_today) / limit))


@router.get("/index/coverage")
async def index_coverage(db: DbSession) -> IndexCoverage:
    settings = get_settings()
    rows = (await db.execute(select(Document.status, func.count()).group_by(Document.status))).all()
    by_status = {s.value: 0 for s in DocumentStatus}
    for status, count in rows:
        by_status[DocumentStatus(status).value] = int(count)
    coverage = await compute_coverage(db)
    today = quota_day(utcnow())
    used = await db.scalar(select(EmbedQuota.used).where(EmbedQuota.day == today)) or 0
    waiting = sum(by_status[s.value] for s in EMBED_WAITING)
    limit = settings.embed_daily_file_limit
    result = IndexCoverage(
        by_status=by_status,
        total_docs=coverage.total_docs,
        bm25_docs=coverage.bm25_docs,
        vector_docs=coverage.vector_docs,
        pending_docs=coverage.pending_docs,
        embed_waiting=waiting,
        daily_limit=limit,
        used_today=int(used),
        estimated_completion=estimate_completion(
            waiting, today=today, limit=limit, used_today=int(used)
        ),
    )
    log.info(
        "index.coverage.read",
        total_docs=result.total_docs,
        bm25_docs=result.bm25_docs,
        vector_docs=result.vector_docs,
        embed_waiting=waiting,
    )
    return result
