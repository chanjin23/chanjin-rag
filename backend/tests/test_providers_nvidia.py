"""NVIDIA Provider: 요청 형식, 재시도, 오류 변환, 추론 텍스트 제거 (TRD 2장, 6.2, 6.3, 7.1).

실제 API는 부르지 않는다. `httpx.MockTransport`로 요청을 받아 형식을 확인하고 응답을 꾸민다.
요청 형식의 기준은 사람이 실제 키로 확인한 `scripts/check_nvidia.py`다.
"""

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from app.core.config import Settings
from app.providers import (
    ChatMessage,
    EmbeddingDimensionError,
    EmbeddingProvider,
    FakeEmbeddingProvider,
    FakeLLMProvider,
    FakeRerankProvider,
    LLMProvider,
    ProviderAuthError,
    ProviderConfigError,
    ProviderUnavailable,
    RateLimited,
    RerankProvider,
    create_embedding_provider,
    create_llm_provider,
    create_rerank_provider,
)
from app.providers.nvidia import (
    NvidiaEmbeddingProvider,
    NvidiaLLMProvider,
    NvidiaRerankProvider,
    RetryPolicy,
    strip_thinking,
)

BASE_URL = "https://integrate.api.nvidia.com/v1"
RERANK_URL = "https://ai.api.nvidia.com/v1/retrieval/nvidia/nemotron-3.5-rerank-8b/reranking"
LLM_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
EMBED_MODEL = "nvidia/nemotron-3.5-embed-8b"
RERANK_MODEL = "nvidia/nemotron-3.5-rerank-8b"
LLM_KEY = "nvapi-test-llm-key-000"
EMBED_KEY = "nvapi-test-embed-key-111"
RERANK_KEY = "nvapi-test-rerank-key-222"
DIM = 8
NO_WAIT = RetryPolicy(initial_wait=0)  # 횟수만 확인하고 기다리지 않는다

Handler = Callable[[httpx.Request], httpx.Response]


class MockApi:
    """요청을 기록하고, 정해 둔 응답(또는 예외)을 순서대로 돌려주는 가짜 서버."""

    def __init__(self, *responses: httpx.Response | Exception | Handler) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # 응답이 모자라면 마지막 것을 계속 쓴다
        r = self.responses[min(len(self.requests), len(self.responses)) - 1]
        if isinstance(r, Exception):
            raise r
        if callable(r) and not isinstance(r, httpx.Response):
            return r(request)
        return r

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def embedding_response(vectors: list[list[float]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": EMBED_MODEL,
            "data": [
                {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )


def embed_echo(request: httpx.Request) -> httpx.Response:
    """요청한 문장 수와 `dimensions`대로 벡터를 돌려준다(문장 i의 벡터는 길이만큼의 같은 값)."""
    body = json.loads(request.content)
    return embedding_response([[float(len(t))] * body["dimensions"] for t in body["input"]])


def status(code: int, text: str = "") -> httpx.Response:
    return httpx.Response(code, json={"detail": text or f"HTTP {code}"})


def sse(*deltas: dict[str, Any]) -> httpx.Response:
    lines = []
    for d in deltas:
        chunk = {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": LLM_MODEL,
            "choices": [{"index": 0, "delta": d, "finish_reason": None}],
        }
        lines.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    lines.append("data: [DONE]\n\n")
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content="".join(lines).encode()
    )


def completion(content: str, reasoning: str | None = None) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-2",
            "object": "chat.completion",
            "created": 0,
            "model": LLM_MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        },
    )


def make_embed(api: MockApi, **kw: Any) -> NvidiaEmbeddingProvider:
    params: dict[str, Any] = {
        "api_key": EMBED_KEY,
        "model": EMBED_MODEL,
        "dim": DIM,
        "base_url": BASE_URL,
        "retry": NO_WAIT,
        "http_client": api.client(),
    }
    params.update(kw)
    return NvidiaEmbeddingProvider(**params)


def make_llm(api: MockApi, **kw: Any) -> NvidiaLLMProvider:
    params: dict[str, Any] = {
        "api_key": LLM_KEY,
        "model": LLM_MODEL,
        "base_url": BASE_URL,
        "temperature": 0.2,
        "max_tokens": 1024,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        "retry": NO_WAIT,
        "http_client": api.client(),
    }
    params.update(kw)
    return NvidiaLLMProvider(**params)


def make_rerank(api: MockApi) -> NvidiaRerankProvider:
    return NvidiaRerankProvider(
        api_key=RERANK_KEY,
        model=RERANK_MODEL,
        url=RERANK_URL,
        retry=NO_WAIT,
        http_client=api.client(),
    )


def provider_calls(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "provider.call"]


@pytest.fixture
def no_secrets_in(request: pytest.FixtureRequest) -> Iterator[list[dict[str, Any]]]:
    """테스트가 남긴 로그 전체에 API 키가 없는지 마지막에 확인한다."""
    with capture_logs() as logs:
        yield logs
    dump = repr(logs)
    for key in (LLM_KEY, EMBED_KEY, RERANK_KEY):
        assert key not in dump


# --------------------------------------------------------------------------- 임베딩


async def test_embedding_request_uses_input_type_and_truncate(
    no_secrets_in: list[dict[str, Any]],
) -> None:
    api = MockApi(embed_echo)
    provider = make_embed(api)
    assert isinstance(provider, EmbeddingProvider)

    passages = await provider.embed(["가나", "다라마"], input_type="passage")
    (query,) = await provider.embed(["위약금"], input_type="query")

    assert passages == [[2.0] * DIM, [3.0] * DIM]
    assert query == [3.0] * DIM
    # EMBED_BATCH_SIZE 기본 1: 문장마다 요청 하나
    assert len(api.requests) == 3
    for req in api.requests:
        assert req.method == "POST"
        assert str(req.url) == f"{BASE_URL}/embeddings"
        assert req.headers["authorization"] == f"Bearer {EMBED_KEY}"

    first, _, last = api.bodies()
    assert first == {
        "model": EMBED_MODEL,
        "input": ["가나"],
        "input_type": "passage",
        "truncate": "END",
        "modality": "text",
        "encoding_format": "float",
        "dimensions": DIM,
    }
    assert last["input_type"] == "query"
    assert last["truncate"] == "END"

    calls = provider_calls(no_secrets_in)
    assert len(calls) == 3
    assert calls[0]["kind"] == "embed"
    assert calls[0]["model"] == EMBED_MODEL
    assert calls[0]["status_code"] == 200
    assert calls[0]["retries"] == 0
    assert calls[0]["input_type"] == "passage"
    assert "latency_ms" in calls[0]
    assert "가나" not in repr(no_secrets_in)  # 본문은 로그에 남기지 않는다


async def test_embedding_batches_and_splits_on_500() -> None:
    def batch_fails(request: httpx.Request) -> httpx.Response:
        # 무료 엔드포인트처럼 2개 이상 묶은 요청에는 500
        if len(json.loads(request.content)["input"]) > 1:
            return status(500, "Internal Server Error")
        return embed_echo(request)

    api = MockApi(batch_fails)
    provider = make_embed(api, batch_size=4)
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]

    with capture_logs() as logs:
        vectors = await provider.embed(texts, input_type="passage")

    assert vectors == [[float(len(t))] * DIM for t in texts]  # 순서 유지
    sizes = [len(b["input"]) for b in api.bodies()]
    # [a,b,c,d] → 500 → [a,b] → 500 → [a],[b] / [c,d] → 500 → [c],[d] / [e]
    assert sizes == [4, 2, 1, 1, 2, 1, 1, 1]
    assert sum(1 for e in logs if e["event"] == "provider.embed.split") == 3


async def test_dimension_mismatch_raises() -> None:
    # dimensions를 무시하고 모델 기본 4096차원을 돌려주는 경우
    api = MockApi(embedding_response([[0.1] * 4096]))
    provider = make_embed(api)

    with pytest.raises(EmbeddingDimensionError) as exc:
        await provider.embed(["문장"], input_type="passage")

    assert "EMBED_DIM" in str(exc.value)
    assert "4096" in str(exc.value)
    assert str(DIM) in str(exc.value)
    assert len(api.requests) == 1  # 형식 오류는 재시도하지 않는다


# --------------------------------------------------------------------------- 재시도


async def test_retries_429_then_succeeds() -> None:
    api = MockApi(status(429, "Too Many Requests"), status(429), embed_echo)
    provider = make_embed(api)

    with capture_logs() as logs:
        (vector,) = await provider.embed(["재시도"], input_type="query")

    assert vector == [3.0] * DIM
    assert len(api.requests) == 3
    (call,) = provider_calls(logs)
    assert call["outcome"] == "ok"
    assert call["status_code"] == 200
    assert call["attempts"] == 3
    assert call["retries"] == 2
    retries = [e for e in logs if e["event"] == "provider.retry"]
    assert [e["status_code"] for e in retries] == [429, 429]

    # 5xx와 타임아웃도 재시도한다(재정렬, LLM)
    rr_api = MockApi(status(503), httpx.ReadTimeout("timeout"), rerank_ok())
    results = await make_rerank(rr_api).rerank("q", ["a", "b"])
    assert [r.index for r in results] == [1, 0]
    assert len(rr_api.requests) == 3

    llm_api = MockApi(status(502), sse({"content": "답"}))
    tokens = [t async for t in make_llm(llm_api).stream([ChatMessage("user", "질문")])]
    assert "".join(tokens) == "답"
    assert len(llm_api.requests) == 2


async def test_gives_up_after_five_attempts() -> None:
    api = MockApi(status(429))
    provider = make_embed(api)
    with capture_logs() as logs, pytest.raises(RateLimited) as exc:
        await provider.embed(["계속 429"], input_type="passage")
    assert len(api.requests) == 5
    assert exc.value.status_code == 429
    (call,) = provider_calls(logs)
    assert call["outcome"] == "error"
    assert call["error"] == "RateLimited"
    assert call["status_code"] == 429
    assert call["retries"] == 4

    api = MockApi(status(503))
    with pytest.raises(ProviderUnavailable) as unavailable:
        await make_rerank(api).rerank("q", ["a"])
    assert len(api.requests) == 5
    assert unavailable.value.status_code == 503

    api = MockApi(httpx.ReadTimeout("timeout"))
    with pytest.raises(ProviderUnavailable):
        await make_llm(api).complete([ChatMessage("user", "질문")])
    assert len(api.requests) == 5

    # 기본 정책: 최대 5회, 최대 대기 60초
    policy = RetryPolicy()
    assert policy.max_attempts == 5
    assert policy.max_wait == 60


async def test_auth_error_not_retried() -> None:
    # NVIDIA는 틀린 키에 401이 아니라 403 "Authorization failed"를 돌려준다
    for code in (403, 401):
        api = MockApi(status(code, "Authorization failed"), embed_echo)
        with capture_logs() as logs, pytest.raises(ProviderAuthError) as exc:
            await make_embed(api).embed(["문장"], input_type="passage")
        assert len(api.requests) == 1
        assert exc.value.status_code == code
        assert EMBED_KEY not in str(exc.value)
        (call,) = provider_calls(logs)
        assert call["error"] == "ProviderAuthError"
        assert call["retries"] == 0

    api = MockApi(status(403, "Authorization failed"))
    with pytest.raises(ProviderAuthError):
        await make_rerank(api).rerank("q", ["a"])
    assert len(api.requests) == 1

    api = MockApi(status(403, "Authorization failed"))
    with pytest.raises(ProviderAuthError):
        _ = [t async for t in make_llm(api).stream([ChatMessage("user", "질문")])]
    assert len(api.requests) == 1


# --------------------------------------------------------------------------- 재정렬


def rerank_ok() -> httpx.Response:
    return httpx.Response(
        200, json={"rankings": [{"index": 1, "logit": 3.5}, {"index": 0, "logit": -2.25}]}
    )


async def test_rerank_request_shape(no_secrets_in: list[dict[str, Any]]) -> None:
    api = MockApi(
        httpx.Response(
            200,
            json={
                "rankings": [
                    {"index": 2, "logit": 5.0},
                    {"index": 0, "logit": 1.5},
                    {"index": 1, "logit": -7.0},
                ]
            },
        )
    )
    provider = make_rerank(api)
    assert isinstance(provider, RerankProvider)

    passages = ["회의실 예약", "서버 비용", "제12조 위약금"]
    results = await provider.rerank("위약금은?", passages)

    (req,) = api.requests
    assert req.method == "POST"
    assert str(req.url) == RERANK_URL
    assert req.headers["authorization"] == f"Bearer {RERANK_KEY}"
    assert req.headers["accept"] == "application/json"
    assert json.loads(req.content) == {
        "model": RERANK_MODEL,
        "query": {"text": "위약금은?"},
        "passages": [{"text": p} for p in passages],
        "truncate": "END",
    }
    assert [(r.index, r.score) for r in results] == [(2, 5.0), (0, 1.5), (1, -7.0)]

    top = await provider.rerank("위약금은?", passages, top_n=1)
    assert [r.index for r in top] == [2]
    assert await provider.rerank("q", []) == []  # 빈 입력은 호출하지 않는다
    assert len(api.requests) == 2

    (call, _) = provider_calls(no_secrets_in)
    assert call["kind"] == "rerank"
    assert call["model"] == RERANK_MODEL
    assert call["items"] == 3


# --------------------------------------------------------------------------- LLM


async def test_llm_strips_thinking_text(no_secrets_in: list[dict[str, Any]]) -> None:
    # 추론이 reasoning_content로 따로 오고, 본문 앞에도 <think> 태그로 조각나 섞여 오는 경우
    api = MockApi(
        sse(
            {"role": "assistant", "content": ""},
            {"reasoning_content": "사용자가 위약금을 묻는다."},
            {"content": "\n<thi"},
            {"content": "nk>계약서 7조를 찾아보자"},
            {"content": "...</th"},
            {"content": "ink>\n\n"},
            {"content": "위약금은 "},
            {"content": "10%입니다 [1]"},
        ),
        completion("<think>짧게 답하자</think>\n재작성된 질문", reasoning="추론"),
        sse({"content": "추론 "}, {"content": "없는 답"}),
    )
    llm = make_llm(api)
    assert isinstance(llm, LLMProvider)
    messages = [ChatMessage("system", "자료로만 답하세요"), ChatMessage("user", "위약금은?")]

    tokens = [t async for t in llm.stream(messages)]
    assert "".join(tokens) == "위약금은 10%입니다 [1]"
    assert await llm.complete(messages, temperature=0.0, max_tokens=64) == "재작성된 질문"
    assert "".join([t async for t in llm.stream(messages)]) == "추론 없는 답"

    stream_body, complete_body, _ = api.bodies()
    assert str(api.requests[0].url) == f"{BASE_URL}/chat/completions"
    assert api.requests[0].headers["authorization"] == f"Bearer {LLM_KEY}"
    assert stream_body["model"] == LLM_MODEL
    assert stream_body["stream"] is True
    assert stream_body["temperature"] == 0.2
    assert stream_body["max_tokens"] == 1024
    assert stream_body["messages"] == [
        {"role": "system", "content": "자료로만 답하세요"},
        {"role": "user", "content": "위약금은?"},
    ]
    # LLM_EXTRA_BODY를 본문에 그대로 합친다
    assert stream_body["chat_template_kwargs"] == {"enable_thinking": False}
    assert complete_body.get("stream") in (None, False)
    assert complete_body["temperature"] == 0.0
    assert complete_body["max_tokens"] == 64

    (call, _, _) = provider_calls(no_secrets_in)
    assert call["kind"] == "llm"
    assert call["op"] == "stream"
    assert call["first_token_ms"] is not None
    assert "위약금" not in repr(no_secrets_in)

    # 태그 처리 경계 사례
    assert strip_thinking("  답변") == "  답변"
    assert strip_thinking("<think>끝나지 않은 추론") == ""
    assert strip_thinking("<thi") == "<thi"
    assert strip_thinking("a <think>b</think> c") == "a <think>b</think> c"  # 앞에 있을 때만 제거


# --------------------------------------------------------------------------- 팩토리


def nvidia_settings(**kw: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "dev",
        "provider": "nvidia",
        "nvidia_llm_api_key": LLM_KEY,
        "nvidia_embed_api_key": EMBED_KEY,
        "nvidia_rerank_api_key": RERANK_KEY,
    }
    values.update(kw)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_factory_selects_provider_by_setting() -> None:
    fake = Settings(_env_file=None, app_env="test", provider="fake")  # type: ignore[call-arg]
    assert isinstance(create_llm_provider(fake), FakeLLMProvider)
    embed = create_embedding_provider(fake)
    assert isinstance(embed, FakeEmbeddingProvider)
    assert embed.dim == fake.embed_dim
    assert isinstance(create_rerank_provider(fake), FakeRerankProvider)

    s = nvidia_settings(
        llm_extra_body={"chat_template_kwargs": {"thinking": False}}, embed_batch_size=4
    )
    llm = create_llm_provider(s)
    assert isinstance(llm, NvidiaLLMProvider)
    assert llm.model == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert llm.extra_body == {"chat_template_kwargs": {"thinking": False}}
    assert (llm.temperature, llm.max_tokens) == (s.llm_temperature, s.llm_max_tokens)
    nv_embed = create_embedding_provider(s)
    assert isinstance(nv_embed, NvidiaEmbeddingProvider)
    assert (nv_embed.dim, nv_embed.batch_size) == (2048, 4)
    rerank = create_rerank_provider(s)
    assert isinstance(rerank, NvidiaRerankProvider)
    assert rerank.url == RERANK_URL
    assert create_rerank_provider(nvidia_settings(rerank_enabled=False)) is None

    # 기본값: 읽기 타임아웃 180초, temperature 0~0.3, max_tokens 1,024~2,048 (TRD 7.1)
    assert s.llm_read_timeout == 180
    assert 0 <= s.llm_temperature <= 0.3
    assert 1024 <= s.llm_max_tokens <= 2048


def test_factory_requires_each_models_own_key() -> None:
    # 다른 모델의 키가 있어도 자기 키가 없으면 만들지 않는다
    cases = [
        (create_llm_provider, "nvidia_llm_api_key", "NVIDIA_LLM_API_KEY"),
        (create_embedding_provider, "nvidia_embed_api_key", "NVIDIA_EMBED_API_KEY"),
        (create_rerank_provider, "nvidia_rerank_api_key", "NVIDIA_RERANK_API_KEY"),
    ]
    for create, field, var in cases:
        with pytest.raises(ProviderConfigError) as exc:
            create(nvidia_settings(**{field: None}))  # type: ignore[operator]
        message = str(exc.value)
        assert var in message
        for key in (LLM_KEY, EMBED_KEY, RERANK_KEY):
            assert key not in message
