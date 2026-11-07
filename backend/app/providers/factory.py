"""`PROVIDER` 설정(fake | nvidia)으로 Provider를 고르는 팩토리 (TRD 12.1, 14장).

API 키는 모델마다 다르므로 LLM은 `NVIDIA_LLM_API_KEY`, 임베딩은 `NVIDIA_EMBED_API_KEY`, 재정렬은
`NVIDIA_RERANK_API_KEY`만 읽는다. 다른 모델의 키로 대신 부르지 않는다. 키가 없으면 해당 Provider를
만들 때 `ProviderConfigError`를 던진다(메시지에는 변수 이름만 있고 키 값은 없다).
"""

from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.providers.base import EmbeddingProvider, LLMProvider, RerankProvider
from app.providers.fake import FakeEmbeddingProvider, FakeLLMProvider, FakeRerankProvider
from app.providers.nvidia import NvidiaEmbeddingProvider, NvidiaLLMProvider, NvidiaRerankProvider


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def create_llm_provider(settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    if s.provider == "fake":
        return FakeLLMProvider()
    return NvidiaLLMProvider(
        api_key=_secret(s.nvidia_llm_api_key),
        model=s.llm_model,
        base_url=s.nvidia_base_url,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
        extra_body=s.llm_extra_body,
        read_timeout=s.llm_read_timeout,
    )


def create_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    s = settings or get_settings()
    if s.provider == "fake":
        return FakeEmbeddingProvider(dim=s.embed_dim)
    return NvidiaEmbeddingProvider(
        api_key=_secret(s.nvidia_embed_api_key),
        model=s.embed_model,
        dim=s.embed_dim,
        base_url=s.nvidia_base_url,
        batch_size=s.embed_batch_size,
        read_timeout=s.provider_read_timeout,
    )


def create_rerank_provider(settings: Settings | None = None) -> RerankProvider | None:
    """`RERANK_ENABLED=false`면 None(재정렬 없이 RRF 순서를 쓴다, TRD 7.1)."""
    s = settings or get_settings()
    if not s.rerank_enabled:
        return None
    if s.provider == "fake":
        return FakeRerankProvider()
    return NvidiaRerankProvider(
        api_key=_secret(s.nvidia_rerank_api_key),
        model=s.rerank_model,
        url=s.rerank_url,
        read_timeout=s.provider_read_timeout,
    )
