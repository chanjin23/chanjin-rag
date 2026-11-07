"""인증 API: POST /auth/login, POST /auth/logout, GET /auth/me (TRD 8.1, 11장).

- 로그인 실패: 401 `invalid_credentials`.
- 잠금(5회 실패, 15분): 429 `login_locked`, 본문에 남은 시간 `retry_after_s`.
- 로그인·로그아웃도 GET이 아니므로 CSRF 검사(`X-Requested-With`, `Origin`)를 거친다.
- 로그 이벤트: `auth.login.succeeded`, `auth.login.failed`, `auth.login.locked`, `auth.logout`.
  아이디와 비밀번호, 쿠키 값은 로그에 남기지 않는다(사용자 ID만).
"""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import (
    SESSION_COOKIE,
    DbSession,
    WebUser,
    clear_session_cookie,
    csrf_protect,
    set_session_cookie,
)
from app.api.errors import ApiError
from app.core import auth
from app.core.logging import get_logger
from app.db.models import User

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1000)


class UserOut(BaseModel):
    id: int
    username: str

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls(id=user.id, username=user.username)


def _locked_error(retry_after_s: int) -> ApiError:
    minutes = -(-retry_after_s // 60)
    return ApiError(
        429,
        "login_locked",
        f"로그인을 여러 번 실패해 잠겼습니다. {minutes}분 뒤에 다시 시도하세요",
        extra={"retry_after_s": retry_after_s},
        headers={"Retry-After": str(retry_after_s)},
    )


@router.post("/login", dependencies=[Depends(csrf_protect)])
async def login(body: LoginIn, response: Response, db: DbSession) -> UserOut:
    now = auth.utcnow()
    result = await auth.authenticate(db, body.username, body.password, now)

    if isinstance(result, auth.LoginOk):
        raw = await auth.create_session(db, result.user, now)
        await db.commit()
        set_session_cookie(response, raw)
        log.info("auth.login.succeeded", user_id=result.user.id)
        return UserOut.of(result.user)

    await db.commit()  # 실패 횟수·잠금 시각을 남긴다
    if isinstance(result, auth.LoginLocked):
        if result.just_locked:
            log.warning("auth.login.failed", user_id=result.user_id, reason="invalid_password")
        log.warning(
            "auth.login.locked",
            user_id=result.user_id,
            retry_after_s=result.retry_after_s,
            just_locked=result.just_locked,
        )
        raise _locked_error(result.retry_after_s)

    log.warning(
        "auth.login.failed",
        user_id=result.user_id,
        reason="unknown_user" if result.user_id is None else "invalid_password",
        failed_logins=result.failed_logins,
    )
    raise ApiError(401, "invalid_credentials", "아이디 또는 비밀번호가 올바르지 않습니다")


@router.post("/logout", status_code=204, dependencies=[Depends(csrf_protect)])
async def logout(request: Request, db: DbSession) -> Response:
    """세션을 지운다. 이미 없거나 만료된 세션이어도 204(쿠키도 지운다)."""
    raw = request.cookies.get(SESSION_COOKIE)
    deleted = False
    if raw:
        deleted = await auth.delete_session(db, raw)
        await db.commit()
    log.info("auth.logout", had_session=deleted)
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.get("/me")
async def me(user: WebUser) -> UserOut:
    return UserOut.of(user)
