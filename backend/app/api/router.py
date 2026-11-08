"""`/api` 아래 라우터 묶음. documents, eval 라우터는 구현할 때 여기에 추가한다.

- 공개: `/health`, `/auth/*`(로그인·로그아웃은 CSRF 검사, `/auth/me`는 세션 필요)
- 웹 API(세션 필수 + CSRF): `web_router` 아래에 붙인다(TRD 8.3)
- Sync API(Bearer 토큰): 라우터마다 `require_sync_token`을 건다(TRD 8.2)
"""

from fastapi import APIRouter

from app.api import auth, chat, health, sync
from app.api.deps import WEB_API_DEPENDENCIES

web_router = APIRouter(dependencies=WEB_API_DEPENDENCIES)
web_router.include_router(chat.router)

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(web_router)
# Sync API(Agent 전용): 라우터 자체에 `require_sync_token`이 걸려 있다
api_router.include_router(sync.router)
