"""NVIDIA API Catalog Provider (TRD 2장 모델 호출, 6.2 재시도, 6.3 임베딩 호출, 7.1 답변 후처리).

- LLM·임베딩: openai SDK(`base_url`=NVIDIA, OpenAI 호환).
- 재정렬: OpenAI 호환이 아니어서 httpx로 호출.
- 요청 형식은 사람이 실제 키로 확인한 `scripts/check_nvidia.py`와 같다.
- 재시도: 429·5xx·타임아웃에 tenacity 지수 백오프(최대 5회, 최대 대기 60초).
  openai SDK 자체 재시도는 끄고(`max_retries=0`) 여기서만 재시도한다.
  끝까지 실패하면 `errors`의 Provider 예외로 내보낸다.
- 로그: 호출마다 `provider.call`(종류, 모델, 지연, 상태 코드, 재시도 횟수).
  요청·응답 본문과 키는 남기지 않는다.
- API 키는 모델마다 다르다. 각 Provider는 생성할 때 받은 자기 키만 쓴다
  (팩토리가 모델별 변수에서 읽음).
"""

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import httpx
import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)
from tenacity.wait import wait_base

from app.core.logging import get_logger
from app.providers.base import ChatMessage, InputType, RerankResult
from app.providers.errors import (
    RETRYABLE,
    EmbeddingDimensionError,
    ProviderConfigError,
    ProviderError,
    ProviderResponseError,
    ProviderUnavailable,
    error_for_status,
)

log = get_logger("app.providers")

T = TypeVar("T")
Kind = Literal["llm", "embed", "rerank"]
KIND_LABELS: dict[Kind, str] = {"llm": "LLM", "embed": "임베딩", "rerank": "재정렬"}

CONNECT_TIMEOUT = 10.0


# --------------------------------------------------------------------------- 재시도


@dataclass(frozen=True)
class RetryPolicy:
    """지수 백오프 설정(TRD 6.2). 테스트는 `initial_wait=0`으로 기다리지 않고 횟수만 확인한다."""

    max_attempts: int = 5
    initial_wait: float = 2.0
    max_wait: float = 60.0

    def wait(self) -> wait_base:
        # 2, 4, 8, 16… 초(최대 60초)에 무작위 지연을 조금 더해 동시에 다시 몰리지 않게 한다
        return wait_exponential(multiplier=self.initial_wait, max=self.max_wait) + wait_random(
            0, self.initial_wait / 2
        )


def to_provider_error(exc: BaseException, kind: Kind) -> ProviderError | None:
    """openai SDK·httpx 예외를 Provider 예외로 바꾼다. 모르는 예외면 None."""
    what = KIND_LABELS[kind]
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, openai.APIStatusError):
        return error_for_status(exc.status_code, what)
    if isinstance(exc, openai.APITimeoutError | httpx.TimeoutException):
        return ProviderUnavailable(f"{what} 응답 시간 초과")
    if isinstance(exc, openai.APIConnectionError | httpx.TransportError):
        return ProviderUnavailable(f"{what} 서버에 연결하지 못했습니다({type(exc).__name__})")
    if isinstance(exc, httpx.HTTPStatusError):
        return error_for_status(exc.response.status_code, what)
    return None


def _default_retryable(e: ProviderError) -> bool:
    return isinstance(e, RETRYABLE)


class _Caller:
    """재시도와 `provider.call` 로그를 맡는 공통 부분."""

    kind: Kind
    model: str
    retry: RetryPolicy

    def _log_retry(self, state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        log.warning(
            "provider.retry",
            kind=self.kind,
            model=self.model,
            attempt=state.attempt_number,
            status_code=getattr(exc, "status_code", None),
            error=type(exc).__name__ if exc else None,
            wait_s=round(state.upcoming_sleep, 2),
        )

    async def _retrying(
        self,
        op: Callable[[], Awaitable[T]],
        retryable: Callable[[ProviderError], bool] = _default_retryable,
    ) -> tuple[T, int]:
        """`op`을 재시도하며 부른다. (결과, 시도 횟수)를 돌려주고, 실패하면 Provider 예외를 던진다.

        던지는 예외에는 `attempts` 속성으로 시도 횟수를 붙인다.
        """
        attempts = 0

        async def attempt() -> T:
            nonlocal attempts
            attempts += 1
            try:
                return await op()
            except Exception as e:
                converted = to_provider_error(e, self.kind)
                if converted is None or converted is e:
                    raise
                raise converted from e

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.retry.max_attempts),
            wait=self.retry.wait(),
            retry=retry_if_exception(lambda e: isinstance(e, ProviderError) and retryable(e)),
            before_sleep=self._log_retry,
            reraise=True,
        )
        try:
            result: T = await retrying(attempt)
        except ProviderError as e:
            e.attempts = attempts  # type: ignore[attr-defined]
            raise
        return result, attempts

    def _log_call(
        self,
        op: str,
        started: float,
        *,
        attempts: int,
        error: BaseException | None = None,
        **fields: Any,
    ) -> None:
        event: dict[str, Any] = {
            "kind": self.kind,
            "op": op,
            "model": self.model,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "status_code": getattr(error, "status_code", None) if error else 200,
            "attempts": attempts,
            "retries": max(attempts - 1, 0),
            "outcome": "error" if error else "ok",
            **fields,
        }
        if error is not None:
            event["error"] = type(error).__name__
            log.warning("provider.call", **event)
        else:
            log.info("provider.call", **event)

    async def _call(
        self,
        op_name: str,
        op: Callable[[], Awaitable[T]],
        *,
        retryable: Callable[[ProviderError], bool] = _default_retryable,
        **fields: Any,
    ) -> T:
        """재시도하며 부르고 결과를 `provider.call`로 남긴다."""
        started = time.perf_counter()
        try:
            result, attempts = await self._retrying(op, retryable)
        except ProviderError as e:
            self._log_call(op_name, started, attempts=getattr(e, "attempts", 1), error=e, **fields)
            raise
        self._log_call(op_name, started, attempts=attempts, **fields)
        return result


def _require(value: str | None, var: str) -> str:
    if not value or not value.strip():
        raise ProviderConfigError(f"{var}가 설정되지 않았습니다. 저장소 루트 .env에 넣으세요")
    return value.strip()


def _openai_client(
    api_key: str, base_url: str, read_timeout: float, http_client: httpx.AsyncClient | None
) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT),
        max_retries=0,  # 재시도는 tenacity가 맡는다
        http_client=http_client,
    )


# --------------------------------------------------------------------------- LLM

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


class ThinkingFilter:
    """답변 앞의 `<think>…</think>` 추론 텍스트를 스트리밍 중에 걸러낸다(TRD 7.1 답변 후처리).

    `reasoning_content` 필드로 따로 오는 추론은 아예 읽지 않는다.
    이 필터는 추론이 답변 본문(`content`) 앞에 태그로 섞여 오는 경우를 처리한다.
    태그가 조각 경계에서 나뉘어 와도 동작한다.
    """

    def __init__(self) -> None:
        self._state: Literal["start", "inside", "after", "answer"] = "start"
        self._buf = ""

    def feed(self, text: str) -> str:
        self._buf += text
        while True:
            if self._state == "start":
                head = self._buf.lstrip()
                if head.startswith(THINK_OPEN):
                    self._buf = head[len(THINK_OPEN) :]
                    self._state = "inside"
                elif THINK_OPEN.startswith(head):  # 아직 태그인지 알 수 없음(빈 문자열 포함)
                    return ""
                else:
                    self._state = "answer"
            elif self._state == "inside":
                i = self._buf.find(THINK_CLOSE)
                if i < 0:
                    # 닫는 태그가 조각 경계에 걸쳤을 수 있어 끝부분만 남긴다
                    self._buf = self._buf[-(len(THINK_CLOSE) - 1) :]
                    return ""
                self._buf = self._buf[i + len(THINK_CLOSE) :]
                self._state = "after"
            elif self._state == "after":
                self._buf = self._buf.lstrip()  # 추론과 답 사이의 줄바꿈
                if not self._buf:
                    return ""
                self._state = "answer"
            else:
                out, self._buf = self._buf, ""
                return out

    def flush(self) -> str:
        """스트림이 끝났을 때 남은 답변. 닫히지 않은 추론은 버린다."""
        out = self._buf if self._state in ("start", "answer") else ""
        self._buf = ""
        return out


def strip_thinking(text: str) -> str:
    f = ThinkingFilter()
    return f.feed(text) + f.flush()


class NvidiaLLMProvider(_Caller):
    kind: Kind = "llm"

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        base_url: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        extra_body: Mapping[str, Any] | None = None,
        read_timeout: float = 180.0,
        retry: RetryPolicy | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        key = _require(api_key, "NVIDIA_LLM_API_KEY")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_body: dict[str, Any] = dict(extra_body or {})
        self.retry = retry or RetryPolicy()
        self._client = _openai_client(key, base_url, read_timeout, http_client)

    def _params(
        self,
        messages: Sequence[ChatMessage],
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        msgs: list[ChatCompletionMessageParam] = [
            {"role": m.role, "content": m.content}  # type: ignore[misc]
            for m in messages
        ]
        return {
            "model": self.model,
            "messages": msgs,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            # LLM_EXTRA_BODY(추론 끄기 등)를 요청 본문에 그대로 합친다
            "extra_body": self.extra_body or None,
        }

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        params = self._params(messages, temperature, max_tokens)
        started = time.perf_counter()

        # 재시도는 응답 스트림을 여는 데까지만 한다. 토큰을 내보낸 뒤에는 다시 보낼 수 없다.
        async def open_stream() -> openai.AsyncStream[Any]:
            s: openai.AsyncStream[Any] = await self._client.chat.completions.create(
                stream=True, **params
            )
            return s

        try:
            response, attempts = await self._retrying(open_stream)
        except ProviderError as e:
            self._log_call("stream", started, attempts=getattr(e, "attempts", 1), error=e)
            raise

        think = ThinkingFilter()
        first_token_ms: int | None = None
        pieces = 0
        error: BaseException | None = None
        try:
            async for chunk in response:
                if not chunk.choices:
                    continue
                # delta.reasoning_content(추론)는 읽지 않는다
                text = think.feed(chunk.choices[0].delta.content or "")
                if text:
                    if first_token_ms is None:
                        first_token_ms = round((time.perf_counter() - started) * 1000)
                    pieces += 1
                    yield text
            rest = think.flush()
            if rest:
                pieces += 1
                yield rest
        except Exception as e:
            converted = to_provider_error(e, self.kind)
            error = converted or e
            if converted is not None and converted is not e:
                raise converted from e
            raise
        finally:
            await response.close()
            self._log_call(
                "stream",
                started,
                attempts=attempts,
                error=error,
                first_token_ms=first_token_ms,
                pieces=pieces,
            )

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        params = self._params(messages, temperature, max_tokens)

        async def op() -> str:
            res = await self._client.chat.completions.create(stream=False, **params)
            if not res.choices:
                raise ProviderResponseError("LLM 응답에 choices가 없습니다")
            return res.choices[0].message.content or ""

        content = await self._call("complete", op)
        return strip_thinking(content)

    async def aclose(self) -> None:
        await self._client.close()


# --------------------------------------------------------------------------- 임베딩


class NvidiaEmbeddingProvider(_Caller):
    kind: Kind = "embed"

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        dim: int,
        base_url: str,
        batch_size: int = 1,
        read_timeout: float = 60.0,
        retry: RetryPolicy | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        key = _require(api_key, "NVIDIA_EMBED_API_KEY")
        if dim < 1:
            raise ProviderConfigError("EMBED_DIM은 1 이상이어야 합니다")
        if batch_size < 1:
            raise ProviderConfigError("EMBED_BATCH_SIZE는 1 이상이어야 합니다")
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.retry = retry or RetryPolicy()
        self._client = _openai_client(key, base_url, read_timeout, http_client)

    async def embed(self, texts: Sequence[str], *, input_type: InputType) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            vectors.extend(
                await self._embed_batch(list(texts[i : i + self.batch_size]), input_type)
            )
        return vectors

    async def _embed_batch(self, batch: list[str], input_type: InputType) -> list[list[float]]:
        split = len(batch) > 1

        def retryable(e: ProviderError) -> bool:
            # 무료 엔드포인트는 묶음 요청에 항상 500을 돌려준다.
            # 같은 묶음을 다시 보내지 않고 반으로 나눈다.
            if split and e.status_code == 500:
                return False
            return _default_retryable(e)

        try:
            return await self._call(
                "embed",
                lambda: self._request(batch, input_type),
                retryable=retryable,
                items=len(batch),
                input_type=input_type,
            )
        except ProviderUnavailable as e:
            if not split or e.status_code != 500:
                raise
        mid = len(batch) // 2
        log.warning(
            "provider.embed.split", model=self.model, items=len(batch), into=[mid, len(batch) - mid]
        )
        first = await self._embed_batch(batch[:mid], input_type)
        return first + await self._embed_batch(batch[mid:], input_type)

    async def _request(self, batch: list[str], input_type: InputType) -> list[list[float]]:
        res = await self._client.embeddings.create(
            model=self.model,
            input=batch,
            encoding_format="float",
            # 모델 기본 4096차원은 halfvec HNSW 한도(4000)를 넘어 줄여 받는다(TRD 6.3)
            dimensions=self.dim,
            extra_body={"input_type": input_type, "truncate": "END", "modality": "text"},
        )
        data = sorted(res.data, key=lambda d: d.index)
        if len(data) != len(batch):
            raise ProviderResponseError(
                f"임베딩 응답 개수({len(data)})가 요청 개수({len(batch)})와 다릅니다"
            )
        for d in data:
            if len(d.embedding) != self.dim:
                raise EmbeddingDimensionError(
                    f"임베딩 응답 차원({len(d.embedding)})이 EMBED_DIM({self.dim})과 다릅니다. "
                    "EMBED_MODEL이 dimensions를 지원하는지, EMBED_DIM 설정을 확인하세요"
                )
        return [list(d.embedding) for d in data]

    async def aclose(self) -> None:
        await self._client.close()


# --------------------------------------------------------------------------- 재정렬


class NvidiaRerankProvider(_Caller):
    kind: Kind = "rerank"

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        url: str | None,
        read_timeout: float = 60.0,
        retry: RetryPolicy | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = _require(api_key, "NVIDIA_RERANK_API_KEY")
        self.url = _require(url, "RERANK_URL")
        self.model = model
        self.retry = retry or RetryPolicy()
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT)
        )

    async def rerank(
        self, query: str, passages: Sequence[str], *, top_n: int | None = None
    ) -> list[RerankResult]:
        if not passages:
            return []
        body = {
            "model": self.model,
            "query": {"text": query},
            "passages": [{"text": p} for p in passages],
            "truncate": "END",
        }

        async def op() -> Any:
            res = await self._http.post(
                self.url,
                json=body,
                headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
            )
            if res.status_code != 200:
                raise error_for_status(res.status_code, KIND_LABELS[self.kind])
            try:
                return res.json()
            except json.JSONDecodeError as e:
                raise ProviderResponseError("재정렬 응답이 JSON이 아닙니다") from e

        data = await self._call("rerank", op, items=len(passages))
        results = self._parse(data, len(passages))
        results.sort(key=lambda r: r.score, reverse=True)
        return results if top_n is None else results[:top_n]

    @staticmethod
    def _parse(data: Any, count: int) -> list[RerankResult]:
        rankings = data.get("rankings") if isinstance(data, dict) else None
        if not isinstance(rankings, list):
            raise ProviderResponseError("재정렬 응답에 rankings가 없습니다")
        results: list[RerankResult] = []
        try:
            for r in rankings:
                index = int(r["index"])
                if not 0 <= index < count:
                    raise ProviderResponseError(f"재정렬 응답의 index({index})가 범위 밖입니다")
                results.append(RerankResult(index=index, score=float(r["logit"])))
        except (KeyError, TypeError, ValueError) as e:
            raise ProviderResponseError("재정렬 응답 형식이 예상과 다릅니다") from e
        return results

    async def aclose(self) -> None:
        await self._http.aclose()
