"""모델 Provider 프로토콜 (TRD 2장 모델 호출, 6.3 임베딩 호출, 14장).

질의 그래프와 Worker는 이 프로토콜에만 기대고, 실제 구현(NVIDIA)과 테스트용 Fake 구현을
`PROVIDER` 설정으로 바꿔 끼운다. LangChain 래퍼는 쓰지 않는다.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]
InputType = Literal["passage", "query"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class RerankResult:
    """재정렬 결과 한 건. `index`는 입력 passages의 위치, 점수가 높을수록 관련 있음."""

    index: int
    score: float


@runtime_checkable
class LLMProvider(Protocol):
    model: str

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """답변을 토큰(텍스트 조각) 단위로 흘려보낸다."""
        ...

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """스트리밍 없이 답변 전체를 돌려준다(질문 재작성, 관련성 판정 등)."""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    model: str
    dim: int

    async def embed(self, texts: Sequence[str], *, input_type: InputType) -> list[list[float]]:
        """텍스트마다 `dim` 차원 벡터를 입력 순서대로 돌려준다.

        문서 청크는 `input_type="passage"`, 질문은 `"query"`로 부른다(TRD 6.3).
        """
        ...


@runtime_checkable
class RerankProvider(Protocol):
    model: str

    async def rerank(
        self, query: str, passages: Sequence[str], *, top_n: int | None = None
    ) -> list[RerankResult]:
        """점수 내림차순으로 정렬한 결과를 돌려준다. `top_n`이 있으면 그만큼만."""
        ...
