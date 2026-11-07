"""결정적인 Fake Provider (TRD 14장). 테스트와 E2E는 항상 이 구현을 쓴다(`PROVIDER=fake`).

- 외부 호출이 없고, 같은 입력에는 항상 같은 결과를 낸다.
- 임베딩: 토큰 해시(feature hashing) 기반 단위 벡터. 같은 텍스트 → 같은 벡터이고,
  토큰을 많이 공유하는 텍스트끼리 코사인 유사도가 높아 검색 테스트에도 쓸 수 있다.
- LLM: 고정 규칙(부분 문자열 → 답변)으로 답하고, 규칙이 없으면 마지막 질문을 되풀이한다.
  답변은 공백을 보존한 단어 조각으로 스트리밍한다.
- 재정렬: 질문 토큰 중 passage에 들어 있는 비율(토큰 겹침 점수).
"""

import asyncio
import hashlib
import math
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from app.providers.base import ChatMessage, InputType, RerankResult

FAKE_LLM_MODEL = "fake-llm"
FAKE_EMBED_MODEL = "fake-embed"
FAKE_RERANK_MODEL = "fake-rerank"
# 규칙에 맞는 것이 없을 때의 답변 머리말
FAKE_ANSWER_PREFIX = "가짜 답변: "

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_PIECE_RE = re.compile(r"\s*\S+\s*")


def tokenize(text: str) -> list[str]:
    """영문은 소문자로 바꾸고 단어 문자 묶음으로 자른다(Fake 전용의 단순 토큰화)."""
    return _TOKEN_RE.findall(text.lower())


def _digest(value: str) -> bytes:
    return hashlib.blake2b(value.encode("utf-8"), digest_size=16).digest()


class FakeEmbeddingProvider:
    def __init__(self, dim: int = 2048, model: str = FAKE_EMBED_MODEL) -> None:
        if dim < 1:
            raise ValueError("dim은 1 이상이어야 합니다")
        self.dim = dim
        self.model = model

    def vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        # 토큰이 없으면(빈 문자열, 기호뿐) 텍스트 전체를 한 토큰으로 본다
        for token in tokenize(text) or [text]:
            d = _digest(token)
            index = int.from_bytes(d[:8], "big") % self.dim
            vec[index] += 1.0 if d[8] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:  # 부호가 상쇄돼 0벡터가 되는 드문 경우
            vec[int.from_bytes(_digest(text)[:8], "big") % self.dim] = 1.0
            return vec
        return [v / norm for v in vec]

    async def embed(self, texts: Sequence[str], *, input_type: InputType) -> list[list[float]]:
        # passage와 query를 같은 공간에 두어, 질문이 같은 내용의 청크와 가깝게 한다
        return [self.vector(t) for t in texts]


@dataclass
class FakeLLMProvider:
    """`rules`는 (부분 문자열, 답변) 목록. 마지막 user 메시지에 처음 맞는 규칙의 답변을 쓴다."""

    rules: Sequence[tuple[str, str]] = ()
    token_delay: float = 0.0
    model: str = FAKE_LLM_MODEL
    calls: list[list[ChatMessage]] = field(default_factory=list)

    def answer_for(self, messages: Sequence[ChatMessage]) -> str:
        question = next((m.content for m in reversed(messages) if m.role == "user"), "")
        for needle, reply in self.rules:
            if needle in question:
                return reply
        return f"{FAKE_ANSWER_PREFIX}{question}"

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        pieces = _PIECE_RE.findall(self.answer_for(messages))
        if max_tokens is not None:
            pieces = pieces[:max_tokens]
        for piece in pieces:
            if self.token_delay:
                await asyncio.sleep(self.token_delay)
            yield piece

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        parts = [
            p async for p in self.stream(messages, temperature=temperature, max_tokens=max_tokens)
        ]
        return "".join(parts)


class FakeRerankProvider:
    def __init__(self, model: str = FAKE_RERANK_MODEL) -> None:
        self.model = model

    @staticmethod
    def score(query: str, passage: str) -> float:
        q = set(tokenize(query))
        if not q:
            return 0.0
        return len(q & set(tokenize(passage))) / len(q)

    async def rerank(
        self, query: str, passages: Sequence[str], *, top_n: int | None = None
    ) -> list[RerankResult]:
        results = [RerankResult(i, self.score(query, p)) for i, p in enumerate(passages)]
        # 점수가 같으면 원래 순서를 유지한다(sorted는 안정 정렬)
        results = sorted(results, key=lambda r: r.score, reverse=True)
        return results if top_n is None else results[:top_n]
