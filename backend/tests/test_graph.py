"""LangGraph 질의 그래프 (PRD 5.2, F-08, F-09, TRD 7.1, 7.3).

실제 테스트 DB에 합성 문서를 넣고 그래프(`run_query`)를 Fake Provider로 실행한다.
Fake LLM 응답기(`app.query.fake_llm`)는 프롬프트의 작업 표식을 보고 결정적으로 답한다.
"""

import json
from collections.abc import Sequence
from typing import Any

from chat_seed import fake_embedder, fake_llm, fake_reranker, seed_corpus, seed_doc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import DocumentStatus, FailureCode
from app.providers.base import ChatMessage
from app.query import prompts
from app.query.fake_llm import fake_query_responder
from app.query.graph import (
    FILTER_EMPTY_TEXT,
    NO_DOCUMENTS_TEXT,
    NOT_FOUND_TEXT,
    QueryDeps,
    QueryResult,
    run_query,
)
from app.query.postprocess import clean_answer
from app.query.search import SearchFilters
from app.query.summary_target import TargetOutcome, resolve_summary_target


async def run(
    session: AsyncSession, question: str, *, llm: Any = None, **kwargs: Any
) -> tuple[list[tuple[str, Any]], QueryResult, QueryDeps]:
    deps = QueryDeps(
        session=session,
        llm=llm or fake_llm(),
        embedder=fake_embedder(),
        reranker=fake_reranker(),
    )
    events: list[tuple[str, Any]] = []
    result: QueryResult | None = None
    async for name, data in run_query(deps, question=question, **kwargs):
        if name == "result":
            result = data
        else:
            events.append((name, data))
    assert result is not None
    return events, result, deps


def stages(events: Sequence[tuple[str, Any]]) -> list[str]:
    return [d["stage"] for n, d in events if n == "status"]


def tasks(llm: Any) -> list[str | None]:
    return [prompts.task_of(call) for call in llm.calls]


async def test_no_history_skips_rewrite(session: AsyncSession) -> None:
    await seed_corpus(session)

    # 이력이 없으면 LLM 재작성을 부르지 않고 질문을 그대로 쓴다
    llm = fake_llm()
    events, result, deps = await run(session, "알파 프로젝트 마감일 알려줘", llm=llm)
    assert prompts.TASK_REWRITE not in tasks(llm)
    assert "rewriting" not in stages(events)
    assert stages(events)[0] == "searching"
    assert deps.trace.rewritten_query == "알파 프로젝트 마감일 알려줘"
    assert result.not_found is False

    # 이력이 있으면 재작성 LLM을 먼저 부르고 rewriting 단계를 알린다
    history = [
        ChatMessage("user", "알파 프로젝트 알아?"),
        ChatMessage("assistant", "알파 프로젝트 담당자는 기획팀이다 [1]"),
    ]
    rewrite_reply = json.dumps(
        {"question": "알파 프로젝트 마감일", "keyword_query": "알파 마감일", "intent": "qa"},
        ensure_ascii=False,
    )

    def responder(messages: Sequence[ChatMessage]) -> str | None:
        if prompts.task_of(messages) == prompts.TASK_REWRITE:
            return rewrite_reply
        return fake_query_responder(messages)

    llm = fake_llm(responder=responder)
    events, _, deps = await run(session, "그거 마감일은?", llm=llm, history=history)
    assert tasks(llm)[0] == prompts.TASK_REWRITE
    assert stages(events)[:2] == ["rewriting", "searching"]
    assert deps.trace.rewritten_query == "알파 프로젝트 마감일"
    assert deps.trace.keyword_query == "알파 마감일"
    assert "rewrite" in deps.trace.latency_ms

    # 재작성 응답이 JSON이 아니면 원문 질문을 쓴다
    def broken(messages: Sequence[ChatMessage]) -> str | None:
        if prompts.task_of(messages) == prompts.TASK_REWRITE:
            return "죄송하지만 JSON을 만들 수 없습니다"
        return fake_query_responder(messages)

    llm = fake_llm(responder=broken)
    _, _, deps = await run(session, "알파 프로젝트 담당자는?", llm=llm, history=history)
    assert tasks(llm)[0] == prompts.TASK_REWRITE
    assert deps.trace.rewritten_query == "알파 프로젝트 담당자는?"
    assert deps.trace.intent == "qa"


async def test_retry_once_then_not_found(session: AsyncSession) -> None:
    await seed_corpus(session)
    llm = fake_llm()

    events, result, deps = await run(session, "화성 기지의 산소 공급량은?", llm=llm)

    # 판정 부족 → 재질의 1회 → 다시 검색·판정 → 그래도 부족하면 찾지 못함
    assert stages(events) == [
        "searching",
        "reranking",
        "grading",
        "retrying",
        "searching",
        "reranking",
        "grading",
    ]
    assert tasks(llm).count(prompts.TASK_RETRY) == 1
    assert tasks(llm).count(prompts.TASK_GRADE) == 2
    assert prompts.TASK_GENERATE not in tasks(llm)
    assert deps.trace.retried is True
    assert deps.trace.grade == "insufficient"
    assert deps.trace.not_found is True
    assert result.not_found is True
    assert result.answer == NOT_FOUND_TEXT

    # 순서: status… → sources(가까운 후보 3개, 인용 번호 없음) → token(고정 문구)
    names = [n for n, _ in events]
    assert names[-2:] == ["sources", "token"]
    sources = events[-2][1]
    assert len(sources) == 3
    assert all(s["n"] is None for s in sources)
    assert events[-1][1] == {"text": NOT_FOUND_TEXT}
    # 검색 범위가 함께 남는다
    assert result.coverage["total_docs"] == 4
    assert result.coverage["vector_docs"] == 4
    # 두 번의 검색 결과가 질의 로그에 시도 번호와 함께 남는다
    assert {e["attempt"] for e in deps.trace.retrieved} == {1, 2}


async def test_invalid_citations_removed(session: AsyncSession) -> None:
    # 단위: 없는 번호 제거, 섞인 번호는 맞는 것만, 추론 텍스트 제거
    assert clean_answer("가 [1]. 나 [3]. 다 [2, 7].", [1, 2]) == "가 [1]. 나. 다 [2]."
    assert clean_answer("<think>먼저 생각</think>\n\n답 [1] [0]", [1]) == "답 [1]"

    await seed_corpus(session)

    def responder(messages: Sequence[ChatMessage]) -> str | None:
        task = prompts.task_of(messages)
        if task == prompts.TASK_GENERATE:
            k = len(prompts.parse_materials(messages[-1].content))
            return (
                "<think>자료를 살펴보자</think>\n"
                f"알파 프로젝트 마감일은 12월 15일이다 [1]. 담당자는 기획팀이다 [{k + 5}]. "
                f"끝 [1, {k + 1}]."
            )
        return fake_query_responder(messages)

    events, result, _ = await run(
        session, "알파 프로젝트 마감일 알려줘", llm=fake_llm(responder=responder)
    )
    sources = next(d for n, d in events if n == "sources")
    k = len(sources)
    assert k >= 1
    assert [s["n"] for s in sources] == list(range(1, k + 1))
    assert result.answer == (
        "알파 프로젝트 마감일은 12월 15일이다 [1]. 담당자는 기획팀이다. 끝 [1]."
    )
    # 화면에 흘려보낸 토큰도 후처리된 결과와 같다
    streamed = "".join(d["text"] for n, d in events if n == "token")
    assert streamed.strip() == result.answer
    assert "<think>" not in streamed
    assert result.cited == [1]


async def test_summarize_target_resolution(session: AsyncSession) -> None:
    ready = await seed_doc(session, r"C:\docs\보고서\분기보고서.md", ["매출이 늘었다."])
    await seed_doc(session, r"C:\docs\a\공통.md", ["가"])
    await seed_doc(session, r"C:\docs\b\공통.md", ["나"])
    queued = await seed_doc(
        session, r"C:\docs\대기문서.pdf", status=DocumentStatus.QUEUED, chunks=False
    )
    extracting = await seed_doc(
        session, r"C:\docs\추출중.docx", status=DocumentStatus.EXTRACTING, chunks=False
    )
    big = await seed_doc(session, r"C:\docs\대용량.pdf", status=DocumentStatus.META_ONLY)
    empty = await seed_doc(session, r"C:\docs\스캔본.pdf", status=DocumentStatus.NO_TEXT)
    broken = await seed_doc(
        session,
        r"C:\docs\암호.docx",
        status=DocumentStatus.FAILED,
        failure_code=FailureCode.ENCRYPTED,
    )

    # 1) 후보 1개, indexed·ready → 요약 진행
    target = await resolve_summary_target(session, "분기보고서.md 요약해줘")
    assert target.outcome == TargetOutcome.READY
    assert target.document is not None and target.document.id == ready.id
    assert target.found_by == "filename"
    # 확장자 없이 이름만 말해도 찾는다
    target = await resolve_summary_target(session, "분기보고서 요약해줘")
    assert target.outcome == TargetOutcome.READY and target.found_by == "filename"

    # 2) 후보 여러 개 → 고르게 함
    target = await resolve_summary_target(session, "공통.md 요약해줘")
    assert target.outcome == TargetOutcome.MULTIPLE
    assert len(target.documents) == 2
    assert "골라" in target.message()

    # 3) 후보 없음 → 찾지 못함 + 비슷한 파일명 3개
    target = await resolve_summary_target(session, "분기보고소.md 요약해줘")
    assert target.outcome == TargetOutcome.NONE
    assert len(target.similar) == 3
    assert target.similar[0] == "분기보고서.md"
    assert target.message().startswith("해당 파일을 찾지 못했습니다")

    # 4) queued·extracting → 아직 처리 중
    for doc in (queued, extracting):
        target = await resolve_summary_target(session, f"{doc.filename} 요약")
        assert target.outcome == TargetOutcome.PROCESSING
        assert "아직 처리 중" in target.message()

    # 5) meta_only·no_text·failed → 요약할 텍스트가 없는 이유
    expected = {big.id: "10MB", empty.id: "텍스트를 추출하지 못했습니다", broken.id: "encrypted"}
    for doc in (big, empty, broken):
        target = await resolve_summary_target(session, f"{doc.filename} 요약해줘")
        assert target.outcome == TargetOutcome.NO_TEXT
        assert expected[doc.id] in target.message()

    # 명시적 선택(summarize_document_id)이 가장 앞서고, 파일명이 없으면 직전 답변의 출처
    target = await resolve_summary_target(
        session, "공통.md 요약해줘", summarize_document_id=ready.id
    )
    assert target.outcome == TargetOutcome.READY and target.found_by == "explicit"
    target = await resolve_summary_target(
        session, "그 문서 요약해줘", previous_document_ids=[ready.id]
    )
    assert target.outcome == TargetOutcome.READY and target.found_by == "previous"

    # 그래프: 여러 개면 status 없이 sources(후보) → token(고르라는 안내)
    events, result, _ = await run(session, "공통.md 요약해줘")
    assert [n for n, _ in events] == ["sources", "token"]
    assert len(events[0][1]) == 2 and all(s["n"] is None for s in events[0][1])
    assert result.not_found is False


async def test_empty_filter_result_message(session: AsyncSession) -> None:
    # 문서가 하나도 없으면 첫 실행 안내
    events, result, deps = await run(session, "위약금 조항 알려줘")
    assert events == [("token", {"text": NO_DOCUMENTS_TEXT})]
    assert result.not_found is True
    assert result.coverage["total_docs"] == 0
    assert deps.trace.saved is True

    # 문서는 있지만 필터에 맞는 문서가 0개면 검색하지 않고 필터 안내(status 없이 token만)
    await seed_corpus(session)
    llm = fake_llm()
    events, result, deps = await run(
        session,
        "위약금 조항 알려줘",
        llm=llm,
        filters=SearchFilters(directory=r"C:\없는폴더"),
    )
    assert events == [("token", {"text": FILTER_EMPTY_TEXT})]
    assert result.not_found is True
    assert result.coverage["total_docs"] == 0
    assert result.coverage["filters_applied"] == {"directory": r"C:\없는폴더"}
    assert llm.calls == []
    assert deps.trace.retrieved == []
    assert deps.trace.not_found is True
