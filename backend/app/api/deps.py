"""FastAPI 공용 의존성: CSRF, 웹 세션, Sync Bearer 토큰 (TRD 8장, 11장).

- 웹 API는 `WebRouter`(=`require_web_user` + `csrf_protect`)를 라우터 의존성으로 건다.
- Sync API(Agent)는 `require_sync_token`. Bearer 헤더는 브라우저가 다른 출처에서 붙일 수 없으므로
  CSRF 검사를 하지 않는다.
- 세션 슬라이딩 갱신 시 쿠키도 다시 보내야 하는데, 엔드포인트가 SSE처럼 Response를 직접 돌려주면
  FastAPI가 의존성의 응답 헤더를 합치지 않는다. 그래서 `request.state`에 표시만 하고
  `app.main`의 미들웨어가 Set-Cookie를 붙인다(`apply_session_cookie`).
"""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.core import auth
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.engine import get_session
from app.db.models import ApiToken, User

log = get_logger(__name__)

SESSION_COOKIE = "sid"
CSRF_HEADER = "X-Requested-With"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

DbSession = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def _origin_allowed(origin: str, host: str | None) -> bool:
    origin = origin.rstrip("/")
    if origin in get_settings().csrf_trusted_origin_list:
        return True
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc or not host:
        return False
    return parts.netloc.lower() == host.lower()


def csrf_protect(request: Request) -> None:
    """GET이 아닌 요청은 `X-Requested-With` 헤더와 같은 출처의 `Origin`이 있어야 한다(TRD 11장)."""
    if request.method in SAFE_METHODS:
        return
    reason: str | None = None
    origin = request.headers.get("origin")
    if not request.headers.get(CSRF_HEADER):
        reason = "missing_header"
    elif not origin:
        reason = "missing_origin"
    elif not _origin_allowed(origin, request.headers.get("host")):
        reason = "origin_mismatch"
    if reason:
        log.warning(
            "auth.csrf.rejected",
            reason=reason,
            method=request.method,
            path=request.url.path,
            origin=origin,
        )
        raise ApiError(403, "csrf_failed", "요청 출처를 확인할 수 없습니다")


# ---------------------------------------------------------------------------
# 웹 세션
# ---------------------------------------------------------------------------


def set_session_cookie(response: Response, raw: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=int(auth.session_ttl().total_seconds()),
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
    )


def apply_session_cookie(request: Request, response: Response) -> None:
    """미들웨어용: 세션을 슬라이딩 갱신한 요청이면 쿠키 만료도 늘려 다시 보낸다."""
    raw = getattr(request.state, "refresh_session_cookie", None)
    if isinstance(raw, str) and "set-cookie" not in response.headers:
        set_session_cookie(response, raw)


async def require_web_user(request: Request, db: DbSession) -> User:
    """세션 쿠키가 없거나 만료·폐기됐으면 401 `unauthenticated`."""
    raw = request.cookies.get(SESSION_COOKIE)
    result: auth.ResolvedSession | str = "missing"
    if raw:
        result = await auth.resolve_session(db, raw, auth.utcnow())
        await db.commit()
    if isinstance(result, str):
        log.info(
            "auth.session.rejected",
            reason=result,
            method=request.method,
            path=request.url.path,
        )
        raise ApiError(401, "unauthenticated", "로그인이 필요합니다")
    if result.refreshed:
        request.state.refresh_session_cookie = raw
    return result.user


WebUser = Annotated[User, Depends(require_web_user)]

# 웹 API 라우터에 거는 의존성 묶음: CSRF 검사 뒤 세션 확인
WEB_API_DEPENDENCIES = [Depends(csrf_protect), Depends(require_web_user)]


# ---------------------------------------------------------------------------
# Sync API (Agent Bearer 토큰)
# ---------------------------------------------------------------------------


async def require_sync_token(request: Request, db: DbSession) -> ApiToken:
    """`Authorization: Bearer <토큰>`을 확인한다.

    없거나 폐기(`revoked_at`)됐으면 401. 성공하면 `last_used_at`을 갱신한다.
    """
    header = request.headers.get("authorization", "")
    scheme, _, raw = header.partition(" ")
    result: ApiToken | str = "missing"
    if scheme.lower() == "bearer" and raw.strip():
        result = await auth.resolve_api_token(db, raw.strip(), auth.utcnow())
        await db.commit()
    if isinstance(result, str):
        log.info("auth.token.rejected", reason=result, path=request.url.path)
        raise ApiError(
            401,
            "invalid_token",
            "API 토큰이 없거나 폐기되었습니다",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return result


SyncToken = Annotated[ApiToken, Depends(require_sync_token)]
