"""`/api` 아래 라우터 묶음. auth, sync, documents, eval 라우터는 구현할 때 여기에 추가한다."""

from fastapi import APIRouter

from app.api import chat, health

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(chat.router)
