"""Provider 호출 결과 기록 (TRD 8.3 `/health`, 12.4 모니터링).

모델 호출 종류(`llm`·`embed`·`rerank`)마다 마지막 성공·실패 시각과 오류 코드를
`provider_status` 테이블에 남긴다. API(채팅)와 Worker(임베딩)는 다른 프로세스라 DB에 둔다.

- `monitored_llm`·`monitored_embedder`·`monitored_reranker`가 Provider를 감싸 결과를 기록한다.
  Provider 예외는 그대로 다시 던진다(호출하는 쪽 동작은 바뀌지 않음).
- 기록은 `ProviderStatusRecorder`가 맡는다. 프로세스 시작 시 `configure_recorder(sessionmaker)`로
  켠다(API lifespan, `rag-backend worker`·`ingest-local`). 켜지 않으면 기록하지 않는다(pytest 기본).
- 성공은 호출마다 쓰지 않고 종류마다 `SUCCESS_WRITE_SECONDS`에 한 번만 쓴다. 단, 직전 기록이
  실패였으면 바로 쓴다(복구를 곧바로 보이게).
- `failing_since`: 같은 오류 코드로 이어진 연속 실패의 첫 시각. 실패가 이어지는 동안은 그대로 두고,
  성공하면 비운다. 배너는 이 시각으로 "같은 장애"를 구분한다(실패마다 다시 뜨지 않음).
- 기록이 실패해도(DB 장애 등) 모델 호출 결과에는 영향을 주지 않는다
  (`provider.status.record_failed`).
- 실패는 `provider.failure.recorded`(종류, 모델, 오류 코드, 상태 코드) 로그로도 남긴다.
"""

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar

from sqlalchemy import and_, case, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models import ProviderStatus
from app.providers.base import (
    ChatMessage,
    EmbeddingProvider,
    InputType,
    LLMProvider,
    RerankProvider,
    RerankResult,
)
from app.providers.errors import ProviderError, provider_error_code

log = get_logger(__name__)

T = TypeVar("T")
Kind = Literal["llm", "embed", "rerank"]
SUCCESS_WRITE_SECONDS = 60.0


class ProviderStatusRecorder:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.clock = clock
        self.monotonic = monotonic
        self._last_success_write: dict[str, float] = {}
        self._failed: set[str] = set()

    async def success(self, kind: Kind, model: str) -> None:
        if self.sessionmaker is None:
            return
        now_m = self.monotonic()
        last = self._last_success_write.get(kind)
        if kind not in self._failed and last is not None and now_m - last < SUCCESS_WRITE_SECONDS:
            return
        self._last_success_write[kind] = now_m
        self._failed.discard(kind)
        now = self.clock()
        await self._write(kind, model, {"last_success_at": now, "failing_since": None}, now)

    async def failure(self, kind: Kind, model: str, exc: ProviderError) -> None:
        code = provider_error_code(exc)
        log.warning(
            "provider.failure.recorded",
            kind=kind,
            model=model,
            error_code=code,
            status_code=exc.status_code,
            recorded=self.sessionmaker is not None,
        )
        if self.sessionmaker is None:
            return
        self._failed.add(kind)
        now = self.clock()
        values = {
            "last_failure_at": now,
            "last_error_code": code,
            "last_status_code": exc.status_code,
        }
        await self._write(kind, model, {**values, "failing_since": now}, now, keep_streak=code)

    async def _write(
        self,
        kind: str,
        model: str,
        values: dict[str, Any],
        now: datetime,
        *,
        keep_streak: str | None = None,
    ) -> None:
        assert self.sessionmaker is not None
        stmt = insert(ProviderStatus).values(kind=kind, model=model, updated_at=now, **values)
        update: dict[str, Any] = {"model": model, "updated_at": now, **values}
        if keep_streak is not None:
            # 이미 같은 코드로 실패 중이면(마지막 실패가 마지막 성공보다 나중)
            # 연속 실패의 첫 시각을 그대로 둔다
            t = ProviderStatus.__table__.c
            still_failing = and_(
                t.failing_since.is_not(None),
                t.last_error_code == keep_streak,
                t.last_failure_at.is_not(None),
                or_(t.last_success_at.is_(None), t.last_failure_at > t.last_success_at),
            )
            update["failing_since"] = case(
                (still_failing, t.failing_since), else_=stmt.excluded.failing_since
            )
        stmt = stmt.on_conflict_do_update(index_elements=[ProviderStatus.kind], set_=update)
        try:
            async with self.sessionmaker() as s, s.begin():
                await s.execute(stmt)
        except Exception as e:  # 기록 실패가 모델 호출을 막지 않게 한다
            log.warning("provider.status.record_failed", kind=kind, error=type(e).__name__)


_recorder = ProviderStatusRecorder()


def get_recorder() -> ProviderStatusRecorder:
    return _recorder


def configure_recorder(sessionmaker: async_sessionmaker[AsyncSession] | None) -> None:
    """이 프로세스의 Provider 호출 결과를 DB에 기록한다(None이면 끈다)."""
    global _recorder
    _recorder = ProviderStatusRecorder(sessionmaker)


class _Monitored:
    """감싼 Provider의 호출 결과를 기록하는 공통 부분."""

    kind: Kind

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.model: str = inner.model

    async def _observe(self, call: Awaitable[T]) -> T:
        """`call`을 기다리며 성공·실패(Provider 예외)를 기록한다. 예외는 그대로 다시 던진다."""
        try:
            result = await call
        except ProviderError as e:
            await get_recorder().failure(self.kind, self.model, e)
            raise
        await get_recorder().success(self.kind, self.model)
        return result

    async def aclose(self) -> None:
        aclose = getattr(self.inner, "aclose", None)
        if aclose is not None:
            await aclose()


class _MonitoredLLM(_Monitored):
    kind: Kind = "llm"

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        # 스트리밍은 마지막 조각까지 받아야 성공이라 `_observe`를 쓰지 않고 같은 규칙으로 기록한다
        try:
            async for token in self.inner.stream(
                messages, temperature=temperature, max_tokens=max_tokens
            ):
                yield token
        except ProviderError as e:
            await get_recorder().failure(self.kind, self.model, e)
            raise
        await get_recorder().success(self.kind, self.model)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        result: str = await self._observe(
            self.inner.complete(messages, temperature=temperature, max_tokens=max_tokens)
        )
        return result


class _MonitoredEmbedder(_Monitored):
    kind: Kind = "embed"

    def __init__(self, inner: EmbeddingProvider) -> None:
        super().__init__(inner)
        self.dim = inner.dim

    async def embed(self, texts: Sequence[str], *, input_type: InputType) -> list[list[float]]:
        result: list[list[float]] = await self._observe(
            self.inner.embed(texts, input_type=input_type)
        )
        return result


class _MonitoredReranker(_Monitored):
    kind: Kind = "rerank"

    async def rerank(
        self, query: str, passages: Sequence[str], *, top_n: int | None = None
    ) -> list[RerankResult]:
        result: list[RerankResult] = await self._observe(
            self.inner.rerank(query, passages, top_n=top_n)
        )
        return result


def monitored_llm(inner: LLMProvider) -> LLMProvider:
    return _MonitoredLLM(inner)


def monitored_embedder(inner: EmbeddingProvider) -> EmbeddingProvider:
    return _MonitoredEmbedder(inner)


def monitored_reranker(inner: RerankProvider | None) -> RerankProvider | None:
    return _MonitoredReranker(inner) if inner is not None else None
