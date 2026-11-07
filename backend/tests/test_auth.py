"""인증: 세션 로그인, 잠금, CSRF, 슬라이딩 만료, Agent 토큰 (TRD 8.1, 11장).

API는 `api_client`(테스트 DB 세션 공유, 끝나면 롤백)로 부른다. 시각을 앞당겨야 하는 경우는
DB의 시각 컬럼을 직접 과거로 옮긴다.
"""

from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.api.deps import SyncToken
from app.api.errors import install_error_handlers
from app.core import auth
from app.core.config import Settings
from app.core.security import sha256_hex, verify_password
from app.db.engine import get_session
from app.db.models import ApiToken, User
from app.db.models import Session as WebSession

CSRF = {"X-Requested-With": "fetch", "Origin": "https://testserver"}
USERNAME = "owner"
PASSWORD = "correct-horse-battery"
TTL = timedelta(days=14)


@pytest.fixture
async def user(session: AsyncSession) -> User:
    return await auth.create_user(session, USERNAME, PASSWORD)


async def _login(
    client: httpx.AsyncClient, password: str = PASSWORD, username: str = USERNAME
) -> httpx.Response:
    return await client.post(
        "/api/auth/login", json={"username": username, "password": password}, headers=CSRF
    )


def _events(logs: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] == name]


async def test_login_sets_secure_session_cookie(
    api_client: httpx.AsyncClient, session: AsyncSession, user: User
) -> None:
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password(user.password_hash, PASSWORD)

    with capture_logs() as logs:
        res = await _login(api_client)
    assert res.status_code == 200
    assert res.json() == {"id": user.id, "username": USERNAME}

    (cookie,) = res.headers.get_list("set-cookie")
    attrs = [a.strip().lower() for a in cookie.split(";")]
    name, _, raw = cookie.split(";", 1)[0].partition("=")
    assert name == "sid"
    assert len(raw) >= 43  # 32바이트 난수(URL-safe base64)
    assert "httponly" in attrs
    assert "secure" in attrs
    assert "samesite=strict" in attrs
    assert "path=/" in attrs
    assert f"max-age={int(TTL.total_seconds())}" in attrs

    # DB에는 쿠키 값의 SHA-256만 저장한다
    rows = (await session.scalars(select(WebSession))).all()
    assert [r.id_hash for r in rows] == [sha256_hex(raw)]
    assert rows[0].user_id == user.id
    assert raw not in rows[0].id_hash
    assert rows[0].expires_at - rows[0].created_at == TTL

    (succeeded,) = _events(logs, "auth.login.succeeded")
    assert succeeded["user_id"] == user.id
    assert raw not in repr(logs) and PASSWORD not in repr(logs)

    # 받은 쿠키로 로그인 상태 확인, 로그아웃하면 세션이 지워진다
    me = await api_client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == USERNAME

    out = await api_client.post("/api/auth/logout", headers=CSRF)
    assert out.status_code == 204
    assert await session.scalar(select(func.count()).select_from(WebSession)) == 0
    assert (await api_client.get("/api/auth/me")).status_code == 401


async def test_session_cookie_secure_flag_follows_setting(
    api_client: httpx.AsyncClient, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """로컬 HTTP(E2E)에서만 Secure를 끌 수 있다. 운영(APP_ENV=prod)은 끌 수 없다."""
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    res = await _login(api_client)
    assert res.status_code == 200
    attrs = [a.strip().lower() for a in res.headers["set-cookie"].split(";")]
    assert "secure" not in attrs
    assert "httponly" in attrs and "samesite=strict" in attrs

    with pytest.raises(ValueError, match="SESSION_COOKIE_SECURE"):
        Settings(_env_file=None, app_env="prod", session_cookie_secure=False)


async def test_lockout_after_five_failures(
    api_client: httpx.AsyncClient, session: AsyncSession, user: User
) -> None:
    with capture_logs() as logs:
        for i in range(1, 5):
            res = await _login(api_client, password="wrong-password")
            assert res.status_code == 401, i
            assert res.json()["error"]["code"] == "invalid_credentials"
            assert user.failed_logins == i

        # 5번째 실패: 15분 잠금
        res = await _login(api_client, password="wrong-password")
        assert res.status_code == 429
        body = res.json()["error"]
        assert body["code"] == "login_locked"
        assert body["retry_after_s"] == 15 * 60
        assert res.headers["retry-after"] == str(15 * 60)
        assert "sid" not in res.cookies

        # 잠긴 동안에는 맞는 비밀번호도 거절하고 남은 시간을 돌려준다
        res = await _login(api_client)
        assert res.status_code == 429
        assert 0 < res.json()["error"]["retry_after_s"] <= 15 * 60
        assert "set-cookie" not in res.headers

    assert len(_events(logs, "auth.login.failed")) == 5
    locked = _events(logs, "auth.login.locked")
    assert [e["just_locked"] for e in locked] == [True, False]
    assert all(e["user_id"] == user.id for e in locked)

    assert user.locked_until is not None
    assert user.locked_until - auth.utcnow() <= timedelta(minutes=15)

    # 15분이 지나면 다시 로그인할 수 있고 실패 횟수가 초기화된다
    user.locked_until = auth.utcnow() - timedelta(seconds=1)
    await session.flush()
    res = await _login(api_client)
    assert res.status_code == 200
    assert user.failed_logins == 0
    assert user.locked_until is None


async def test_lockout_counts_consecutive_failures_only(
    api_client: httpx.AsyncClient, user: User
) -> None:
    """성공하면 실패 횟수가 초기화된다. 없는 아이디는 잠그지 않고 같은 401을 준다."""
    for _ in range(4):
        assert (await _login(api_client, password="nope")).status_code == 401
    assert (await _login(api_client)).status_code == 200
    assert user.failed_logins == 0
    for _ in range(4):
        assert (await _login(api_client, password="nope")).status_code == 401
    assert user.locked_until is None

    res = await _login(api_client, username="nobody", password="nope")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_credentials"


async def test_csrf_rejects_missing_header_or_origin(
    api_client: httpx.AsyncClient, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"username": USERNAME, "password": PASSWORD}
    cases = {
        "missing_header": {"Origin": "https://testserver"},
        "missing_origin": {"X-Requested-With": "fetch"},
        "origin_mismatch": {"X-Requested-With": "fetch", "Origin": "https://evil.example"},
    }
    with capture_logs() as logs:
        for headers in cases.values():
            res = await api_client.post("/api/auth/login", json=body, headers=headers)
            assert res.status_code == 403, headers
            assert res.json()["error"]["code"] == "csrf_failed"
            assert "set-cookie" not in res.headers
    assert [e["reason"] for e in _events(logs, "auth.csrf.rejected")] == list(cases)

    # 헤더가 모두 맞으면 로그인된다
    assert (await _login(api_client)).status_code == 200

    # 세션이 있어도 웹 API의 GET이 아닌 요청은 CSRF 검사를 거친다
    res = await api_client.post("/api/conversations/c1/messages", json={"text": "안녕"})
    assert res.status_code == 403
    res = await api_client.post(
        "/api/conversations/c1/messages",
        json={"text": "안녕"},
        headers={"X-Requested-With": "fetch", "Origin": "https://evil.example"},
    )
    assert res.status_code == 403
    res = await api_client.post("/api/auth/logout")
    assert res.status_code == 403

    # GET은 검사하지 않는다
    assert (await api_client.get("/api/auth/me")).status_code == 200

    # 프록시가 Host를 바꾸는 배포를 위해 설정한 출처는 허용한다
    monkeypatch.setenv("CSRF_TRUSTED_ORIGINS", "https://rag.example.ts.net/")
    from app.core.config import get_settings

    get_settings.cache_clear()
    res = await api_client.post(
        "/api/auth/logout",
        headers={"X-Requested-With": "fetch", "Origin": "https://rag.example.ts.net"},
    )
    assert res.status_code == 204


async def test_session_sliding_expiry(
    api_client: httpx.AsyncClient, session: AsyncSession, user: User
) -> None:
    assert (await _login(api_client)).status_code == 200
    web_session = await session.scalar(select(WebSession))
    assert web_session is not None

    # 1분 안의 요청은 행을 다시 쓰지 않는다(쿠키도 다시 보내지 않음)
    res = await api_client.get("/api/auth/me")
    assert res.status_code == 200
    assert "set-cookie" not in res.headers

    # 13일 동안 쓰지 않았다가 다시 쓰면 만료가 그 시점부터 14일로 늘어난다
    web_session.last_seen_at = auth.utcnow() - timedelta(days=13)
    web_session.expires_at = auth.utcnow() + timedelta(days=1)
    await session.flush()
    before = auth.utcnow()
    res = await api_client.get("/api/auth/me")
    assert res.status_code == 200
    attrs = [a.strip().lower() for a in res.headers["set-cookie"].split(";")]
    assert f"max-age={int(TTL.total_seconds())}" in attrs
    assert web_session.expires_at >= before + TTL
    assert web_session.last_seen_at >= before

    # SSE처럼 Response를 직접 돌려주는 웹 API도 갱신된 쿠키를 받는다
    web_session.last_seen_at = auth.utcnow() - timedelta(days=1)
    await session.flush()
    res = await api_client.post(
        "/api/conversations/c1/messages", json={"text": "안녕"}, headers=CSRF
    )
    assert res.status_code == 200
    assert "sid=" in res.headers.get("set-cookie", "")

    # 마지막 사용에서 14일이 지나면 만료: 401, 세션 행 삭제
    web_session.expires_at = auth.utcnow() - timedelta(seconds=1)
    await session.flush()
    with capture_logs() as logs:
        res = await api_client.get("/api/auth/me")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthenticated"
    assert [e["reason"] for e in _events(logs, "auth.session.rejected")] == ["expired"]
    assert await session.scalar(select(func.count()).select_from(WebSession)) == 0

    # 서비스 함수 단위: 시각을 직접 넘겨 확인
    t0 = auth.utcnow()
    raw = await auth.create_session(session, user, t0)
    r1 = await auth.resolve_session(session, raw, t0 + timedelta(seconds=30))
    assert isinstance(r1, auth.ResolvedSession) and not r1.refreshed
    r2 = await auth.resolve_session(session, raw, t0 + timedelta(days=10))
    assert isinstance(r2, auth.ResolvedSession) and r2.refreshed
    assert r2.session.expires_at == t0 + timedelta(days=10) + TTL
    # 처음 만든 뒤 14일이 지났어도 중간에 썼으므로 아직 유효
    r3 = await auth.resolve_session(session, raw, t0 + timedelta(days=20))
    assert isinstance(r3, auth.ResolvedSession)
    r4 = await auth.resolve_session(session, raw, t0 + timedelta(days=20) + TTL)
    assert r4 == "expired"


async def test_bearer_token_revoked_rejected(session: AsyncSession) -> None:
    probe = FastAPI()
    install_error_handlers(probe)

    @probe.get("/sync/probe")
    async def sync_probe(token: SyncToken) -> dict[str, int]:
        return {"token_id": token.id}

    async def _session():  # type: ignore[no-untyped-def]
        yield session

    probe.dependency_overrides[get_session] = _session
    token, raw = await auth.create_api_token(session, "집 PC")
    assert token.token_hash == sha256_hex(raw)
    assert len(raw) >= 43
    assert token.last_used_at is None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=probe), base_url="https://testserver"
    ) as client:
        ok = await client.get("/sync/probe", headers={"Authorization": f"Bearer {raw}"})
        assert ok.status_code == 200
        assert ok.json() == {"token_id": token.id}
        assert token.last_used_at is not None

        with capture_logs() as logs:
            missing = await client.get("/sync/probe")
            wrong = await client.get("/sync/probe", headers={"Authorization": "Bearer nope"})
            token.revoked_at = auth.utcnow()
            await session.flush()
            used_at = token.last_used_at
            revoked = await client.get("/sync/probe", headers={"Authorization": f"Bearer {raw}"})

    for res in (missing, wrong, revoked):
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "invalid_token"
        assert res.headers["www-authenticate"] == "Bearer"
    assert token.last_used_at == used_at  # 폐기된 토큰은 사용 시각도 갱신하지 않는다
    reasons = [e["reason"] for e in _events(logs, "auth.token.rejected")]
    assert reasons == ["missing", "unknown", "revoked"]
    assert raw not in repr(logs)

    stored = (await session.scalars(select(ApiToken.token_hash))).all()
    assert raw not in stored
