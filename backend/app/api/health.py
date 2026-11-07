"""GET /api/health. 지금은 프로세스 생존만 확인한다.

DB, Worker 하트비트, 디스크 여유 확인은 이후 추가한다 (TRD 12.4).
"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
