"""모델 호출 Provider 인터페이스 (TRD 1장 설계 원칙: 모델 교체 가능).

NVIDIA 구현(`nvidia.py`)과 테스트용 Fake 구현(`fake.py`)은 구현 단계에서 추가한다.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


class LLMProvider(Protocol):
    def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]: ...


class EmbeddingProvider(Protocol):
    async def embed(
        self, texts: Sequence[str], input_type: Literal["passage", "query"]
    ) -> list[list[float]]: ...


@dataclass(frozen=True)
class RerankResult:
    index: int
    score: float


class RerankProvider(Protocol):
    async def rerank(self, query: str, passages: Sequence[str]) -> list[RerankResult]: ...
