"""상태 점검 `/api/health`(공개 요약)·`/api/health/details`(세션) (TRD 8.3, 12.4):
DB, Worker 하트비트, 디스크 여유, Provider 상태, 배너용 이상(`since`·`level`)과 로그."""

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool
from structlog.testing import capture_logs

from app.api import health as health_api
from app.core.auth import utcnow
from app.db.engine import create_engine, create_sessionmaker, get_session
from app.db.models import ProviderStatus, WorkerHeartbeat
from app.providers.base import ChatMessage
from app.providers.errors import (
    ProviderAuthError,
    ProviderQuotaExhausted,
    RateLimited,
    error_for_status,
    provider_error_code,
)
from app.providers.fake import FakeLLMProvider
from app.providers.monitor import (
    ProviderStatusRecorder,
    configure_recorder,
    get_recorder,
    monitored_llm,
)

GB = 1024**3


def _fake_disk(free_gb: float) -> Any:
    def usage(path: Path) -> health_api.DiskUsage:
        total = 100 * GB
        free = int(free_gb * GB)
        return health_api.DiskUsage(total, total - free, free)

    return usage


def _disk_error(path: Path) -> health_api.DiskUsage:
    raise OSError("no such device")


@pytest.fixture(autouse=True)
def fresh_tracker(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """이상 발견 시각·마지막 로그 기록을 테스트마다 비운다. 디스크는 기본 50GB."""
    monkeypatch.setattr(health_api, "tracker", health_api.IssueTracker())
    monkeypatch.setattr(health_api, "disk_usage", _fake_disk(50))
    yield


async def _details(client: httpx.AsyncClient) -> dict[str, Any]:
    res = await client.get("/api/health/details")
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


def _codes(body: dict[str, Any]) -> list[str]:
    return [i["code"] for i in body["issues"]]


async def _heartbeat(session: AsyncSession, age: timedelta) -> None:
    now = utcnow()
    hb = await session.get(WorkerHeartbeat, "worker")
    if hb is None:
        session.add(
            WorkerHeartbeat(
                name="worker", started_at=now - timedelta(hours=1), heartbeat_at=now - age
            )
        )
    else:
        hb.heartbeat_at = now - age
    await session.flush()


async def test_health_reports_worker_provider_disk(
    chat_client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = utcnow()

    # 1) 정상: 하트비트 30초 전, Provider 성공만(예전 429 뒤 성공), 디스크 50GB
    await _heartbeat(session, timedelta(seconds=30))
    session.add(
        ProviderStatus(kind="llm", model="m-llm", last_success_at=now - timedelta(minutes=5))
    )
    session.add(
        ProviderStatus(
            kind="embed",
            model="m-embed",
            last_success_at=now - timedelta(minutes=1),
            last_failure_at=now - timedelta(minutes=10),
            last_error_code="rate_limited",
            last_status_code=429,
        )
    )
    await session.flush()
    ok = await _details(chat_client)
    assert ok["status"] == "ok"
    assert ok["degraded"] is False and ok["issues"] == []
    assert ok["checks"]["db"]["ok"] is True
    assert ok["checks"]["worker"]["status"] == "ok"
    assert ok["checks"]["worker"]["name"] == "worker"
    assert 25 <= ok["checks"]["worker"]["age_seconds"] < 90
    assert ok["checks"]["disk"]["status"] == "ok"
    assert ok["checks"]["disk"]["free_bytes"] == 50 * GB
    providers = {p["kind"]: p for p in ok["checks"]["providers"]}
    # 예전 실패(429) 뒤에 성공했으면 이상이 아니다. 마지막 실패 시각·코드는 그대로 보여준다
    assert providers["embed"]["failing"] is False
    assert providers["embed"]["last_error_code"] == "rate_limited"
    assert providers["embed"]["last_status_code"] == 429
    assert providers["embed"]["last_failure_at"] is not None
    assert providers["llm"]["last_success_at"] is not None and providers["llm"]["failing"] is False

    # 공개 요약은 상태와 이상 코드만(세부 없음)
    public = (await chat_client.get("/api/health")).json()
    assert public == {"status": "ok", "degraded": False, "issues": []}

    # 2) 이상: 하트비트 2분 전(90초 넘음), LLM 인증 오류(403) 연속 실패, 디스크 5GB
    await _heartbeat(session, timedelta(minutes=2))
    llm = await session.get(ProviderStatus, "llm")
    assert llm is not None
    llm.last_failure_at = now - timedelta(minutes=1)
    llm.failing_since = now - timedelta(minutes=3)
    llm.last_error_code = "provider_auth"
    llm.last_status_code = 403
    await session.flush()
    monkeypatch.setattr(health_api, "disk_usage", _fake_disk(5.5))
    with capture_logs() as logs:
        bad = await _details(chat_client)
        again = await _details(chat_client)  # 같은 이상은 다시 기록하지 않는다
    assert bad["status"] == "ok" and bad["degraded"] is True
    assert bad["checks"]["worker"]["status"] == "stale"
    assert bad["checks"]["disk"]["status"] == "low"
    providers = {p["kind"]: p for p in bad["checks"]["providers"]}
    assert providers["llm"]["failing"] is True
    assert providers["llm"]["last_error_code"] == "provider_auth"
    assert providers["llm"]["last_status_code"] == 403
    codes = _codes(bad)
    assert codes == ["worker_stale", "disk_low", "provider_auth"]
    issues = {i["code"]: i for i in bad["issues"]}
    assert issues["disk_low"]["level"] == 5  # 남은 GB(내림)
    assert issues["disk_low"]["since"] is not None  # 처음 발견한 시각
    auth = issues["provider_auth"]
    assert "인증" in auth["message"]
    # since는 연속 실패의 첫 시각(마지막 실패 시각이 아님) → 실패가 이어져도 배너 키가 같다
    assert auth["since"] == providers["llm"]["failing_since"]
    assert auth["since"] != providers["llm"]["last_failure_at"]
    assert again["issues"] == bad["issues"]
    degraded = [e for e in logs if e["event"] == "health.degraded"]
    assert len(degraded) == 1
    assert degraded[0]["issues"] == codes
    assert degraded[0]["providers"] == {"llm": "provider_auth"}
    public = (await chat_client.get("/api/health")).json()
    assert public == {"status": "ok", "degraded": True, "issues": [{"code": c} for c in codes]}

    # 디스크가 더 줄면 level이 바뀐다(닫은 배너가 다시 보이게). since(첫 발견)는 그대로
    monkeypatch.setattr(health_api, "disk_usage", _fake_disk(3.2))
    worse = {i["code"]: i for i in (await _details(chat_client))["issues"]}
    assert worse["disk_low"]["level"] == 3
    assert worse["disk_low"]["since"] == issues["disk_low"]["since"]

    # 3) 하트비트 기록이 아예 없으면 worker_missing. since는 처음 발견한 시각으로 유지
    await session.execute(delete(WorkerHeartbeat))
    await session.flush()
    missing = await _details(chat_client)
    assert missing["checks"]["worker"]["status"] == "missing"
    first = {i["code"]: i for i in missing["issues"]}["worker_missing"]
    assert first["since"] is not None
    later = {i["code"]: i for i in (await _details(chat_client))["issues"]}["worker_missing"]
    assert later["since"] == first["since"]


@pytest.mark.parametrize(("age_s", "expected"), [(89, "ok"), (91, "stale")])
async def test_worker_heartbeat_90_second_boundary(
    chat_client: httpx.AsyncClient, session: AsyncSession, age_s: int, expected: str
) -> None:
    await _heartbeat(session, timedelta(seconds=age_s))
    body = await _details(chat_client)
    assert body["checks"]["worker"]["status"] == expected
    assert body["checks"]["worker"]["stale_after_seconds"] == 90
    assert ("worker_stale" in _codes(body)) is (expected == "stale")


async def test_health_recovered_logged_when_issues_clear(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await _heartbeat(session, timedelta(minutes=5))
    with capture_logs() as logs:
        assert _codes(await _details(chat_client)) == ["worker_stale"]
        await _heartbeat(session, timedelta(seconds=5))
        assert _codes(await _details(chat_client)) == []
        await _details(chat_client)  # 정상이 이어지면 다시 남기지 않는다
    events = [e["event"] for e in logs if e["event"].startswith("health.")]
    assert events == ["health.degraded", "health.recovered"]
    recovered = next(e for e in logs if e["event"] == "health.recovered")
    assert recovered["previous"] == ["worker_stale"]


class _BrokenSession:
    """연결이 끊긴 DB 세션 흉내: 쿼리마다 예외."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError("db down")

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError("db down")

    async def scalars(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError("db down")

    async def rollback(self) -> None:
        return None


async def test_health_db_unavailable_and_disk_unknown(
    chat_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.main import app

    async def broken() -> AsyncIterator[Any]:
        yield _BrokenSession()

    previous = app.dependency_overrides[get_session]
    app.dependency_overrides[get_session] = broken
    monkeypatch.setattr(health_api, "disk_usage", _disk_error)
    try:
        with capture_logs() as logs:
            body = await _details(chat_client)
            public = await chat_client.get("/api/health")
    finally:
        app.dependency_overrides[get_session] = previous
    assert body["status"] == "ok" and body["degraded"] is True
    assert body["checks"]["db"] == {"ok": False, "error": "ConnectionRefusedError"}
    assert body["checks"]["worker"]["status"] == "unknown"
    assert body["checks"]["providers"] == []
    # 디스크를 확인하지 못하면 unknown(이상으로 보지 않음)
    assert body["checks"]["disk"]["status"] == "unknown"
    assert body["checks"]["disk"]["free_bytes"] is None
    assert _codes(body) == ["db_unavailable"]
    assert body["issues"][0]["since"] is not None
    # 공개 요약도 200(프로세스는 살아 있음)
    assert public.status_code == 200
    assert public.json() == {
        "status": "ok",
        "degraded": True,
        "issues": [{"code": "db_unavailable"}],
    }
    degraded = [e for e in logs if e["event"] == "health.degraded"]
    assert len(degraded) == 1 and degraded[0]["issues"] == ["db_unavailable"]


async def test_credit_exhaustion_shows_provider_auth_issue(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """402(크레딧·사용량 소진)도 키 오류와 같은 `provider_auth`로 배너에 뜬다(TRD 12.4)."""
    err = error_for_status(402, "임베딩")
    assert isinstance(err, ProviderQuotaExhausted) and isinstance(err, ProviderAuthError)
    assert err.status_code == 402
    assert provider_error_code(err) == "provider_auth"
    assert provider_error_code(error_for_status(403, "LLM")) == "provider_auth"
    assert provider_error_code(RateLimited("x", status_code=429)) == "rate_limited"

    now = utcnow()
    await _heartbeat(session, timedelta(seconds=10))
    session.add(
        ProviderStatus(
            kind="embed",
            model="m-embed",
            last_success_at=now - timedelta(hours=1),
            last_failure_at=now - timedelta(minutes=1),
            failing_since=now - timedelta(minutes=1),
            last_error_code="provider_auth",
            last_status_code=402,
        )
    )
    await session.flush()
    body = await _details(chat_client)
    assert _codes(body) == ["provider_auth"]
    assert "크레딧" in body["issues"][0]["message"] and "embed" in body["issues"][0]["message"]


async def test_health_details_requires_session(api_client: httpx.AsyncClient) -> None:
    res = await api_client.get("/api/health/details")
    assert res.status_code == 401
    public = await api_client.get("/api/health")
    assert public.status_code == 200 and "checks" not in public.json()


async def test_monitored_provider_records_failure_streak_and_recovery(migrated_db: str) -> None:
    """Provider 래퍼가 실패(코드·상태)·연속 실패의 첫 시각·복구(성공)를 provider_status에 남긴다."""
    engine = create_engine(migrated_db, poolclass=NullPool)
    sessionmaker = create_sessionmaker(engine)
    fail: dict[str, Exception | None] = {"error": ProviderAuthError("fake", status_code=403)}

    def responder(messages: Any) -> str | None:
        if fail["error"] is not None:
            raise fail["error"]
        return "답"

    async def row() -> ProviderStatus:
        async with sessionmaker() as s:
            r = await s.scalar(select(ProviderStatus).where(ProviderStatus.kind == "llm"))
            assert r is not None
            return r

    llm = monitored_llm(FakeLLMProvider(responder=responder, model="m-test"))
    configure_recorder(sessionmaker)
    try:
        with pytest.raises(ProviderAuthError):
            await llm.complete([ChatMessage("user", "질문")])
        first = await row()
        assert first.model == "m-test" and first.last_error_code == "provider_auth"
        assert first.last_status_code == 403 and first.last_success_at is None
        assert first.failing_since is not None

        # 같은 코드로 다시 실패: 마지막 실패 시각만 바뀌고 연속 실패의 첫 시각은 그대로
        with pytest.raises(ProviderAuthError):
            await llm.complete([ChatMessage("user", "질문")])
        second = await row()
        assert second.last_failure_at is not None and first.last_failure_at is not None
        assert second.last_failure_at > first.last_failure_at
        assert second.failing_since == first.failing_since

        # 다른 코드로 실패하면 새 연속 실패
        fail["error"] = RateLimited("fake", status_code=429)
        with pytest.raises(RateLimited):
            await llm.complete([ChatMessage("user", "질문")])
        third = await row()
        assert third.last_error_code == "rate_limited"
        assert third.failing_since == third.last_failure_at

        # 성공하면 복구: 연속 실패 시각을 비운다
        fail["error"] = None
        assert await llm.complete([ChatMessage("user", "질문")]) == "답"
        healed = await row()
        assert healed.last_success_at is not None and healed.last_failure_at is not None
        assert healed.last_success_at > healed.last_failure_at
        assert healed.failing_since is None

        # 다시 실패하면 새 시각으로 시작(배너가 다시 뜬다)
        fail["error"] = ProviderAuthError("fake", status_code=403)
        with pytest.raises(ProviderAuthError):
            await llm.complete([ChatMessage("user", "질문")])
        again = await row()
        assert again.failing_since is not None and first.failing_since is not None
        assert again.failing_since > first.failing_since
    finally:
        configure_recorder(None)
        async with sessionmaker() as s, s.begin():
            await s.execute(delete(ProviderStatus))
        await engine.dispose()


async def test_recorder_without_sessionmaker_is_noop() -> None:
    recorder = ProviderStatusRecorder()
    await recorder.success("llm", "m")
    await recorder.failure("llm", "m", ProviderAuthError("x"))
    assert get_recorder().sessionmaker is None
