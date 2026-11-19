"""인증 서비스: 로그인 잠금, 웹 세션, Agent 토큰, 사용자 관리 (TRD 5.2, 8.1, 11장).

- 함수는 넘겨받은 AsyncSession에서 읽고 쓰기만 하고 commit은 호출하는 쪽(API, CLI)이 한다.
- 시각은 항상 UTC aware datetime(`now`)을 넘겨받는다. 테스트가 시각을 정할 수 있게 하기 위해서다.
- 평문 비밀번호, 쿠키 값, 토큰은 로그에 남기지 않는다.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import (
    burn_password_check,
    hash_password,
    new_token,
    sha256_hex,
    verify_password,
)
from app.db.models import ApiToken, User
from app.db.models import Session as WebSession

# 요청마다 세션 행을 갱신하지 않고, 마지막 갱신에서 이만큼 지났을 때만 만료 시각을 늘린다.
SESSION_TOUCH_INTERVAL = timedelta(minutes=1)


def utcnow() -> datetime:
    return datetime.now(UTC)


class AuthError(Exception):
    """CLI 등에서 사용자에게 그대로 보여줄 수 있는 오류."""


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoginOk:
    user: User


@dataclass(frozen=True)
class LoginInvalid:
    user_id: int | None  # 없는 사용자면 None
    failed_logins: int


@dataclass(frozen=True)
class LoginLocked:
    user_id: int
    retry_after_s: int
    just_locked: bool  # 이번 실패로 잠겼으면 True, 이미 잠겨 있었으면 False


LoginResult = LoginOk | LoginInvalid | LoginLocked


def _retry_after(locked_until: datetime, now: datetime) -> int:
    return max(1, math.ceil((locked_until - now).total_seconds()))


async def authenticate(
    session: AsyncSession, username: str, password: str, now: datetime
) -> LoginResult:
    """아이디·비밀번호를 확인한다. 연속 실패가 한도에 닿으면 잠근다(TRD 11장: 5회, 15분).

    잠긴 동안에는 비밀번호를 확인하지 않는다(맞는 비밀번호여도 잠금 유지).
    """
    settings = get_settings()
    user = await session.scalar(select(User).where(User.username == username).with_for_update())
    if user is None:
        burn_password_check(password)
        return LoginInvalid(user_id=None, failed_logins=0)

    if user.locked_until is not None and user.locked_until > now:
        return LoginLocked(
            user_id=user.id,
            retry_after_s=_retry_after(user.locked_until, now),
            just_locked=False,
        )

    if verify_password(user.password_hash, password):
        user.failed_logins = 0
        user.locked_until = None
        await session.flush()
        return LoginOk(user=user)

    user.failed_logins += 1
    if user.failed_logins >= settings.login_max_failures:
        user.failed_logins = 0
        user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
        await session.flush()
        return LoginLocked(
            user_id=user.id,
            retry_after_s=_retry_after(user.locked_until, now),
            just_locked=True,
        )
    await session.flush()
    return LoginInvalid(user_id=user.id, failed_logins=user.failed_logins)


# ---------------------------------------------------------------------------
# 웹 세션
# ---------------------------------------------------------------------------


def session_ttl() -> timedelta:
    return timedelta(days=get_settings().session_ttl_days)


async def create_session(session: AsyncSession, user: User, now: datetime) -> str:
    """세션을 만들고 쿠키에 넣을 평문 값을 돌려준다. DB에는 SHA-256만 저장한다."""
    raw = new_token()
    session.add(
        WebSession(
            id_hash=sha256_hex(raw),
            user_id=user.id,
            expires_at=now + session_ttl(),
            last_seen_at=now,
            created_at=now,
        )
    )
    await session.flush()
    return raw


@dataclass(frozen=True)
class ResolvedSession:
    user: User
    session: WebSession
    refreshed: bool  # 이번 요청에서 만료 시각을 늘렸으면 True(쿠키도 다시 보냄)


async def resolve_session(session: AsyncSession, raw: str, now: datetime) -> ResolvedSession | str:
    """쿠키 값으로 세션을 찾는다. 실패하면 이유(`unknown`, `expired`)를 돌려준다.

    14일 슬라이딩 만료: 마지막 갱신에서 1분 넘게 지났으면 `expires_at = now + 14일`로 늘린다.
    만료된 세션은 지운다.
    """
    row = (
        await session.execute(
            select(WebSession, User)
            .join(User, User.id == WebSession.user_id)
            .where(WebSession.id_hash == sha256_hex(raw))
        )
    ).first()
    if row is None:
        return "unknown"
    web_session, user = row
    if web_session.expires_at <= now:
        await session.delete(web_session)
        await session.flush()
        return "expired"

    refreshed = False
    if now - web_session.last_seen_at >= SESSION_TOUCH_INTERVAL:
        web_session.last_seen_at = now
        web_session.expires_at = now + session_ttl()
        await session.flush()
        refreshed = True
    return ResolvedSession(user=user, session=web_session, refreshed=refreshed)


async def delete_session(session: AsyncSession, raw: str) -> bool:
    result = await session.execute(delete(WebSession).where(WebSession.id_hash == sha256_hex(raw)))
    return bool(getattr(result, "rowcount", 0))


# ---------------------------------------------------------------------------
# Agent 토큰
# ---------------------------------------------------------------------------


async def create_api_token(session: AsyncSession, name: str) -> tuple[ApiToken, str]:
    """토큰을 발급한다. 평문은 이 반환값으로 한 번만 주고 DB에는 SHA-256만 저장한다."""
    raw = new_token()
    token = ApiToken(name=name, token_hash=sha256_hex(raw))
    session.add(token)
    await session.flush()
    return token, raw


async def resolve_api_token(session: AsyncSession, raw: str, now: datetime) -> ApiToken | str:
    """Bearer 토큰을 확인하고 `last_used_at`을 갱신한다. 실패하면 이유(`unknown`, `revoked`)."""
    token = await session.scalar(select(ApiToken).where(ApiToken.token_hash == sha256_hex(raw)))
    if token is None:
        return "unknown"
    if token.revoked_at is not None:
        return "revoked"
    token.last_used_at = now
    if token.first_used_at is None:
        token.first_used_at = now
    await session.flush()
    return token


# ---------------------------------------------------------------------------
# 사용자 (CLI 전용)
# ---------------------------------------------------------------------------


def _check_password_policy(password: str) -> None:
    if len(password) < 8:
        raise AuthError("비밀번호는 8자 이상이어야 합니다")


async def create_user(session: AsyncSession, username: str, password: str) -> User:
    """사용자를 만든다. 단일 계정이라 이미 사용자가 있으면 거절한다(TRD 5.2)."""
    username = username.strip()
    if not username:
        raise AuthError("아이디가 비어 있습니다")
    _check_password_policy(password)
    count = await session.scalar(select(func.count()).select_from(User))
    if count:
        raise AuthError(
            "이미 사용자가 있습니다(단일 계정). 비밀번호를 바꾸려면 reset-password를 쓰세요"
        )
    user = User(username=username, password_hash=hash_password(password))
    session.add(user)
    await session.flush()
    return user


async def reset_password(session: AsyncSession, username: str, password: str) -> User:
    """비밀번호를 바꾸고 잠금을 풀며, 그 사용자의 기존 세션을 모두 지운다."""
    _check_password_policy(password)
    user = await session.scalar(select(User).where(User.username == username.strip()))
    if user is None:
        raise AuthError(f"사용자가 없습니다: {username}")
    user.password_hash = hash_password(password)
    user.failed_logins = 0
    user.locked_until = None
    await session.execute(delete(WebSession).where(WebSession.user_id == user.id))
    await session.flush()
    return user
