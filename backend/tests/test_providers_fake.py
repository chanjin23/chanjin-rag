"""Fake Provider가 결정적으로 동작하고 프로토콜을 만족하는지 확인한다 (TRD 14장)."""

import math

from app.core.config import get_settings
from app.providers import (
    ChatMessage,
    EmbeddingProvider,
    FakeEmbeddingProvider,
    FakeLLMProvider,
    FakeRerankProvider,
    LLMProvider,
    RerankProvider,
)


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


async def test_fake_embedding_is_deterministic() -> None:
    dim = get_settings().embed_dim
    assert dim == 2048
    provider = FakeEmbeddingProvider(dim=dim)
    assert isinstance(provider, EmbeddingProvider)

    texts = ["위약금 조항은 계약서 7조에 있다", "점심 메뉴 추천", "위약금 조항은 계약서 7조에 있다"]
    first = await provider.embed(texts, input_type="passage")
    second = await FakeEmbeddingProvider(dim=dim).embed(texts, input_type="passage")

    assert first == second  # 다른 인스턴스, 다른 호출이어도 같은 텍스트 → 같은 벡터
    assert first[0] == first[2]
    assert first[0] != first[1]
    assert all(len(v) == dim for v in first)
    assert all(math.isclose(_norm(v), 1.0) for v in first)

    # 질문 벡터는 같은 내용의 청크와 가깝고, 무관한 청크와는 멀다
    (query,) = await provider.embed(["위약금 조항"], input_type="query")
    assert _cos(query, first[0]) > _cos(query, first[1])

    # 빈 문자열도 0벡터가 아닌 단위 벡터
    (empty,) = await provider.embed([""], input_type="query")
    assert math.isclose(_norm(empty), 1.0)

    # 차원은 설정을 따른다
    (small,) = await FakeEmbeddingProvider(dim=8).embed(["a"], input_type="query")
    assert len(small) == 8


async def test_fake_llm_streams_tokens() -> None:
    llm = FakeLLMProvider(rules=[("위약금", "위약금은 계약 금액의 10%입니다 [1]")])
    assert isinstance(llm, LLMProvider)

    messages = [
        ChatMessage("system", "자료로만 답하세요"),
        ChatMessage("user", "작년 계약서의 위약금 조항 알려줘"),
    ]
    tokens = [t async for t in llm.stream(messages)]
    assert len(tokens) > 1  # 한 번에 오지 않고 여러 조각으로 온다
    assert "".join(tokens) == "위약금은 계약 금액의 10%입니다 [1]"
    assert llm.calls[-1] == messages

    # 규칙에 없으면 마지막 질문을 되풀이하는 고정 응답. 여러 번 불러도 같다
    other = [ChatMessage("user", "오늘 회의 요약")]
    assert await llm.complete(other) == "가짜 답변: 오늘 회의 요약"
    assert await llm.complete(other) == await llm.complete(other)

    limited = [t async for t in llm.stream(other, max_tokens=2)]
    assert len(limited) == 2


async def test_fake_rerank_scores_token_overlap() -> None:
    reranker = FakeRerankProvider()
    assert isinstance(reranker, RerankProvider)

    passages = ["점심 메뉴", "계약서 위약금 조항 설명", "위약금 안내"]
    results = await reranker.rerank("위약금 조항", passages)
    assert [r.index for r in results] == [1, 2, 0]
    assert [r.score for r in results] == [1.0, 0.5, 0.0]
    top = await reranker.rerank("위약금 조항", passages, top_n=1)
    assert [r.index for r in top] == [1]
