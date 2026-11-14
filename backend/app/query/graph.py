"""LangGraph 질의 그래프 (PRD 5.2, F-08, F-09, TRD 7.1, 7.3).

```
scope ─(범위 0개)──────────────────────────────────────────────┐
  └→ rewrite ─(summarize)→ summarize ───────────────────────────┤
       └(qa)→ retrieve → rerank → grade ─(충분)→ generate ──────┤→ log → END
                 ↑                   ├(부족, 첫 시도)→ retry_rewrite
                 └───────────────────┘(부족, 재시도 후)→ not_found ┘
```

- `scope`(TRD 7.1 "필터 결과 0개, 문서 0개"): 필터를 적용한 문서가 0개면 검색하지 않고 안내 문구로
  끝낸다. 이때는 TRD 7.3 순서표대로 `status` 없이 `token` → `done`만 나가도록 그래프 맨 앞에서 본다.
- 노드는 `get_stream_writer()`로 SSE 이벤트(`status`, `sources`, `token`)를 흘려보낸다
  (`astream(stream_mode=["custom", "values"])`의 custom 모드). `done`·`error`는 호출한 쪽(API)이
  최종 상태와 예외로 만든다. LangChain 채팅 모델을 쓰지 않으므로 messages 모드는 쓰지 않는다.
- 의존성(DB 세션, Provider)은 실행 컨텍스트(`context_schema=QueryDeps`)로 넘긴다. 그래프는 한 번만
  컴파일해 재사용한다.
- 질의 로그에 남길 값(단계별 지연, 후보 점수, 판정, 모델명)은 `QueryTrace`에 모은다. 중간에 끊기거나
  실패해도 API가 그때까지 모은 값으로 질의 로그를 남길 수 있게 상태와 따로 둔다.
"""

import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Chunk, QueryLog, Section
from app.providers.base import ChatMessage, EmbeddingProvider, LLMProvider, RerankProvider
from app.providers.errors import ProviderError
from app.providers.nvidia import strip_thinking
from app.query import prompts
from app.query.postprocess import AnswerFilter, cited_numbers
from app.query.search import (
    MAX_PER_DOCUMENT,
    Coverage,
    SearchFilters,
    SearchHit,
    compute_coverage,
    search,
)
from app.query.summary_target import (
    SummaryTarget,
    TargetDoc,
    TargetOutcome,
    resolve_summary_target,
    wants_summary,
)

log = get_logger(__name__)

TOP_K = 8  # 재정렬 뒤 생성에 넣는 청크 수(TRD 7.1)
NOT_FOUND_CANDIDATES = 3
MAX_ATTEMPTS = 2  # 첫 검색 + 재검색 1회
SNIPPET_CHARS = 200
# 요약: 부분 하나의 최대 글자 수와 부분 수 상한(넘으면 부분마다 앞부분만 쓴다)
SUMMARY_PART_CHARS = 8000
SUMMARY_MAX_PARTS = 12
# 짧은 JSON 판단 호출 설정
JUDGE_MAX_TOKENS = 300

NOT_FOUND_TEXT = "문서에서 찾지 못했습니다."
FILTER_EMPTY_TEXT = (
    "필터 조건에 맞는 문서가 없습니다. 필터를 바꾸거나 해제한 뒤 다시 질문해 주세요."
)
NO_DOCUMENTS_TEXT = (
    "아직 색인된 문서가 없습니다. 첫 실행 체크리스트에서 토큰 발급, Agent 설치, 첫 동기화를 "
    "마친 뒤 다시 질문해 주세요."
)

Stage = Literal[
    "rewriting", "searching", "reranking", "grading", "retrying", "summarizing", "generating"
]
Intent = Literal["qa", "summarize"]


@dataclass
class QueryTrace:
    """질의 로그(`query_logs`)에 남길 값. 노드가 채우고 `log` 노드나 API가 저장한다."""

    started: float = field(default_factory=time.perf_counter)
    rewritten_query: str | None = None
    keyword_query: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    grade: str | None = None
    retried: bool = False
    not_found: bool = False
    coverage: dict[str, Any] = field(default_factory=dict)
    latency_ms: dict[str, float] = field(default_factory=dict)
    models: dict[str, str | None] = field(default_factory=dict)
    intent: str | None = None
    saved: bool = False
    # 질의 출처(`chat`, `eval`)와 저장된 질의 로그 ID
    source: str = "chat"
    log_id: int | None = None

    def add_latency(self, key: str, ms: float) -> None:
        self.latency_ms[key] = round(self.latency_ms.get(key, 0.0) + ms, 2)

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 2)


@dataclass
class QueryDeps:
    session: AsyncSession
    llm: LLMProvider
    embedder: EmbeddingProvider | None
    reranker: RerankProvider | None
    trace: QueryTrace = field(default_factory=QueryTrace)
    top_k: int = TOP_K
    temperature: float | None = None
    max_tokens: int | None = None


class QueryState(TypedDict, total=False):
    question: str
    history: list[ChatMessage]
    previous_document_ids: list[int]
    filters: SearchFilters
    summarize_document_id: int | None
    message_id: int | None
    rewritten: str
    keyword_query: str | None
    intent: Intent
    target_document_id: int | None
    candidates: list[SearchHit]
    selected: list[SearchHit]
    grade: Literal["sufficient", "insufficient"] | None
    grade_reason: str
    attempt: int
    coverage: Coverage
    scope_empty: bool
    answer: str
    citations: list[dict[str, Any]]
    not_found: bool


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _status(stage: Stage, progress: tuple[int, int] | None = None) -> None:
    data: dict[str, Any] = {"stage": stage}
    if progress is not None:
        data["progress"] = {"done": progress[0], "total": progress[1]}
    get_stream_writer()({"event": "status", "data": data})


def _emit(event: str, data: Any) -> None:
    get_stream_writer()({"event": event, "data": data})


def _snippet(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= SNIPPET_CHARS else flat[:SNIPPET_CHARS] + "…"


def hit_source(n: int | None, hit: SearchHit) -> dict[str, Any]:
    """SSE `sources` 항목이자 `messages.citations` 항목(TRD 5.3, 7.3)."""
    return {
        "n": n,
        "chunk_id": hit.chunk_id,
        "document_id": hit.document_id,
        "path": hit.path,
        "location": hit.location,
        "mtime": hit.mtime.isoformat(),
        "snippet": hit.snippet,
        "locally_deleted": hit.locally_deleted,
    }


def doc_source(n: int | None, doc: TargetDoc, snippet: str = "") -> dict[str, Any]:
    return {
        "n": n,
        "chunk_id": None,
        "document_id": doc.id,
        "path": doc.path,
        "location": {},
        "mtime": doc.mtime.isoformat(),
        "snippet": snippet,
        "locally_deleted": doc.locally_deleted,
    }


def hit_material(n: int, hit: SearchHit) -> prompts.Material:
    return prompts.Material(
        n=n,
        filename=hit.filename,
        location=prompts.format_location(hit.location),
        mtime=hit.mtime,
        text=hit.content,
    )


def select_top(
    hits: Sequence[SearchHit], k: int, per_document: int = MAX_PER_DOCUMENT
) -> list[SearchHit]:
    """순서대로 최대 k개, 문서당 최대 `per_document`개."""
    out: list[SearchHit] = []
    per_doc: dict[int, int] = {}
    for h in hits:
        if per_doc.get(h.document_id, 0) >= per_document:
            continue
        per_doc[h.document_id] = per_doc.get(h.document_id, 0) + 1
        out.append(h)
        if len(out) >= k:
            break
    return out


async def _stream_answer(deps: QueryDeps, messages: list[ChatMessage], valid: Sequence[int]) -> str:
    """LLM 답변을 후처리하며 `token` 이벤트로 흘려보내고 전체 답을 돌려준다."""
    f = AnswerFilter(valid)
    parts: list[str] = []
    first = True

    def out(text: str) -> None:
        nonlocal first
        if not text:
            return
        if first:
            deps.trace.latency_ms["first_token"] = deps.trace.elapsed_ms()
            first = False
        parts.append(text)
        _emit("token", {"text": text})

    async for piece in deps.llm.stream(
        messages, temperature=deps.temperature, max_tokens=deps.max_tokens
    ):
        out(f.feed(piece))
    out(f.flush())
    if f.citations.removed:
        log.info("query.citations.removed", count=f.citations.removed)
    return "".join(parts).strip()


def _fixed_answer(deps: QueryDeps, text: str) -> str:
    deps.trace.latency_ms.setdefault("first_token", deps.trace.elapsed_ms())
    _emit("token", {"text": text})
    return text


# ---------------------------------------------------------------------------
# 노드
# ---------------------------------------------------------------------------


async def scope_node(state: QueryState, runtime: Runtime[QueryDeps]) -> dict[str, Any]:
    deps = runtime.context
    t0 = time.perf_counter()
    filters = state.get("filters") or SearchFilters()
    deps.trace.filters = filters.model_dump(mode="json")
    coverage = await compute_coverage(deps.session, filters)
    deps.trace.coverage = coverage.as_dict()
    update: dict[str, Any] = {"coverage": coverage, "scope_empty": False, "attempt": 0}
    # 문서를 직접 고른 요약 요청은 필터와 상관없이 진행한다
    if coverage.total_docs == 0 and state.get("summarize_document_id") is None:
        overall = coverage if not coverage.filters_applied else await compute_coverage(deps.session)
        text = FILTER_EMPTY_TEXT if overall.total_docs > 0 else NO_DOCUMENTS_TEXT
        deps.trace.not_found = True
        update.update(
            scope_empty=True, answer=_fixed_answer(deps, text), citations=[], not_found=True
        )
        log.info(
            "query.scope.empty",
            reason="filter" if overall.total_docs > 0 else "no_documents",
            filters=sorted(coverage.filters_applied),
        )
    deps.trace.add_latency("scope", _ms(t0))
    return update


async def rewrite_node(state: QueryState, runtime: Runtime[QueryDeps]) -> dict[str, Any]:
    deps = runtime.context
    question = state["question"]
    history = state.get("history") or []
    explicit = state.get("summarize_document_id") is not None
    heuristic: Intent = "summarize" if explicit or wants_summary(question) else "qa"
    rewritten, keyword, intent = question, None, heuristic
    if history:
        # 이력이 있을 때만 LLM으로 독립 질문을 만든다(TRD 7.1, 7.4)
        _status("rewriting")
        t0 = time.perf_counter()
        reply = await deps.llm.complete(
            prompts.rewrite_messages(question, history),
            temperature=0.0,
            max_tokens=JUDGE_MAX_TOKENS,
        )
        deps.trace.add_latency("rewrite", _ms(t0))
        parsed = prompts.parse_json_object(reply)
        if parsed is None:
            log.warning("query.rewrite.unparsed")
        else:
            q = parsed.get("question")
            if isinstance(q, str) and q.strip():
                rewritten = q.strip()
            kw = parsed.get("keyword_query")
            if isinstance(kw, str) and kw.strip():
                keyword = kw.strip()
            if parsed.get("intent") in ("qa", "summarize") and not explicit:
                intent = parsed["intent"]
    deps.trace.rewritten_query = rewritten
    deps.trace.keyword_query = keyword
    deps.trace.intent = intent
    return {"rewritten": rewritten, "keyword_query": keyword, "intent": intent, "attempt": 0}


async def retrieve_node(state: QueryState, runtime: Runtime[QueryDeps]) -> dict[str, Any]:
    deps = runtime.context
    attempt = state.get("attempt", 0) + 1
    _status("searching")
    t0 = time.perf_counter()
    result = await search(
        deps.session,
        deps.embedder,
        state["rewritten"],
        filters=state.get("filters") or SearchFilters(),
        keyword_query=state.get("keyword_query"),
    )
    # 검색 트랜잭션(SET LOCAL 포함)을 닫아, 생성하는 동안 연결을 붙잡지 않게 한다
    await deps.session.commit()
    deps.trace.add_latency("retrieve", _ms(t0))
    for key in ("embed", "sql"):
        if key in result.latency_ms:
            deps.trace.add_latency(key, result.latency_ms[key])
    deps.trace.coverage = result.coverage.as_dict()
    deps.trace.retrieved.extend({**h.log_entry(), "attempt": attempt} for h in result.hits)
    return {"candidates": result.hits, "coverage": result.coverage, "attempt": attempt}


async def rerank_node(state: QueryState, runtime: Runtime[QueryDeps]) -> dict[str, Any]:
    deps = runtime.context
    candidates = state.get("candidates") or []
    ordered = list(candidates)
    attempt = state.get("attempt", 1)
    if deps.reranker is not None and candidates:
        _status("reranking")
        t0 = time.perf_counter()
        try:
            results = await deps.reranker.rerank(
                state["rewritten"], [h.content for h in candidates]
            )
        except ProviderError as e:
            # 재정렬이 실패해도 RRF 순서로 계속한다
            log.warning("query.rerank.failed", error=type(e).__name__)
        else:
            scores = {r.index: r.score for r in results}
            ordered = [candidates[r.index] for r in results if 0 <= r.index < len(candidates)]
            by_chunk = {candidates[i].chunk_id: s for i, s in scores.items() if i < len(candidates)}
            for entry in deps.trace.retrieved:
                if entry.get("attempt") == attempt and entry["chunk_id"] in by_chunk:
                    entry["rerank"] = round(by_chunk[entry["chunk_id"]], 6)
        deps.trace.add_latency("rerank", _ms(t0))
    selected = select_top(ordered, deps.top_k)
    chosen = {h.chunk_id for h in selected}
    for entry in deps.trace.retrieved:
        if entry.get("attempt") == attempt:
            entry["selected"] = entry["chunk_id"] in chosen
    return {"selected": selected}


async def grade_node(state: QueryState, runtime: Runtime[QueryDeps]) -> dict[str, Any]:
    deps = runtime.context
    selected = state.get("selected") or []
    if not selected:
        deps.trace.grade = "insufficient"
        return {"grade": "insufficient", "grade_reason": "검색 결과 없음"}
    _status("grading")
    t0 = time.perf_counter()
    materials = [hit_material(i, h) for i, h in enumerate(selected, start=1)]
    reply = await deps.llm.complete(
        prompts.grade_messages(state["rewritten"], materials),
        temperature=0.0,
        max_tokens=JUDGE_MAX_TOKENS,
    )
    deps.trace.add_latency("grade", _ms(t0))
    parsed = prompts.parse_json_object(reply)
    sufficient = True
    reason = ""
    if parsed is None or not isinstance(parsed.get("sufficient"), bool):
        # 판정을 읽지 못하면 근거가 있다고 보고 생성으로 넘긴다(생성 단계도 근거 밖 답을 막는다)
        log.warning("query.grade.unparsed")
    else:
        sufficient = bool(parsed["sufficient"])
        reason = str(parsed.get("reason") or "")[:300]
    grade: Literal["sufficient", "insufficient"] = "sufficient" if sufficient else "insufficient"
    deps.trace.grade = grade
    return {"grade": grade, "grade_reason": reason}


async def retry_rewrite_node(state: QueryState, runtime: Runtime[QueryDeps]) -> dict[str, Any]:
    deps = runtime.context
    _status("retrying")
    t0 = time.perf_counter()
    reply = await deps.llm.complete(
        prompts.retry_messages(state["rewritten"], state.get("grade_reason", "")),
        temperature=0.0,
        max_tokens=JUDGE_MAX_TOKENS,
    )
    deps.trace.add_latency("retry_rewrite", _ms(t0))
    deps.trace.retried = True
    rewritten = state["rewritten"]
    keyword = state.get("keyword_query")
    parsed = prompts.parse_json_object(reply)
    if parsed is None:
        log.warning("query.retry.unparsed")
    else:
        q = parsed.get("query")
        if isinstance(q, str) and q.strip():
            rewritten = q.strip()
        kw = parsed.get("keyword_query")
        if isinstance(kw, str) and kw.strip():
            keyword = kw.strip()
    deps.trace.rewritten_query = rewritten
    deps.trace.keyword_query = keyword
    return {"rewritten": rewritten, "keyword_query": keyword}


async def generate_node(state: QueryState, runtime: Runtime[QueryDeps]) -> dict[str, Any]:
    deps = runtime.context
    selected = state.get("selected") or []
    _status("generating")
    sources = [hit_source(i, h) for i, h in enumerate(selected, start=1)]
    _emit("sources", sources)
    t0 = time.perf_counter()
    materials = [hit_material(i, h) for i, h in enumerate(selected, start=1)]
    answer = await _stream_answer(
        deps,
        prompts.generate_messages(state["rewritten"], materials, state.get("history") or []),
        range(1, len(selected) + 1),
    )
    deps.trace.add_latency("generate", _ms(t0))
    return {"answer": answer, "citations": sources, "not_found": False}


async def not_found_node(state: QueryState, runtime: Runtime[QueryDeps]) -> dict[str, Any]:
    deps = runtime.context
    nearest = (state.get("selected") or state.get("candidates") or [])[:NOT_FOUND_CANDIDATES]
    # 가까웠던 후보는 인용 번호 없이 참고용으로만 보낸다(n=null)
    sources = [hit_source(None, h) for h in nearest]
    _emit("sources", sources)
    deps.trace.not_found = True
    return {"answer": _fixed_answer(deps, NOT_FOUND_TEXT), "citations": sources, "not_found": True}


async def _document_text_parts(session: AsyncSession, document_id: int) -> list[str]:
    """요약할 문서 본문을 부분(최대 `SUMMARY_PART_CHARS`자)으로 나눈다. sections가 없으면 chunks."""
    texts = list(
        (
            await session.execute(
                select(Section.text).where(Section.document_id == document_id).order_by(Section.seq)
            )
        ).scalars()
    )
    if not texts:
        texts = list(
            (
                await session.execute(
                    select(Chunk.content)
                    .where(Chunk.document_id == document_id)
                    .order_by(Chunk.seq)
                )
            ).scalars()
        )
    parts: list[str] = []
    current = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        while len(t) > SUMMARY_PART_CHARS:
            if current:
                parts.append(current)
                current = ""
            parts.append(t[:SUMMARY_PART_CHARS])
            t = t[SUMMARY_PART_CHARS:]
        if current and len(current) + len(t) + 2 > SUMMARY_PART_CHARS:
            parts.append(current)
            current = ""
        current = f"{current}\n\n{t}" if current else t
    if current:
        parts.append(current)
    if len(parts) > SUMMARY_MAX_PARTS:
        # 아주 긴 문서: 이웃한 부분을 묶어 부분 수를 줄이고, 묶음마다 앞부분만 쓴다
        size = -(-len(parts) // SUMMARY_MAX_PARTS)
        log.info("query.summary.truncated", parts=len(parts), max_parts=SUMMARY_MAX_PARTS)
        parts = [
            "\n\n".join(parts[i : i + size])[:SUMMARY_PART_CHARS]
            for i in range(0, len(parts), size)
        ]
    return parts


async def summarize_node(state: QueryState, runtime: Runtime[QueryDeps]) -> dict[str, Any]:
    deps = runtime.context
    t0 = time.perf_counter()
    question = state["question"]
    probe = (
        question
        if state.get("rewritten", question) == question
        else (f"{question}\n{state['rewritten']}")
    )
    target: SummaryTarget = await resolve_summary_target(
        deps.session,
        probe,
        summarize_document_id=state.get("summarize_document_id"),
        previous_document_ids=state.get("previous_document_ids") or [],
    )
    log.info(
        "query.summary.target",
        outcome=target.outcome.value,
        found_by=target.found_by,
        candidates=len(target.documents),
    )
    update: dict[str, Any] = {"not_found": False}
    doc = target.document
    if target.outcome != TargetOutcome.READY or doc is None:
        if target.outcome == TargetOutcome.NONE:
            sources: list[dict[str, Any]] = []
            update["not_found"] = True
            deps.trace.not_found = True
        else:
            # 여러 개면 고를 후보 목록, 처리 중·텍스트 없음이면 해당 문서(인용 번호 없음)
            sources = [doc_source(None, d) for d in target.documents]
            _emit("sources", sources)
        update.update(answer=_fixed_answer(deps, target.message()), citations=sources)
        deps.trace.add_latency("summarize", _ms(t0))
        return update

    parts = await _document_text_parts(deps.session, doc.id)
    await deps.session.commit()
    total = len(parts)
    _status("summarizing", (0, total))
    summaries: list[str] = []
    if total > 1:
        for i, part in enumerate(parts, start=1):
            reply = await deps.llm.complete(
                prompts.summarize_map_messages(question, doc.filename, i, total, part),
                temperature=deps.temperature,
            )
            summaries.append(strip_thinking(reply).strip())
            _status("summarizing", (i, total))
    sources = [doc_source(1, doc, _snippet(parts[0]) if parts else "")]
    _emit("sources", sources)
    messages = prompts.summarize_reduce_messages(
        question,
        doc.filename,
        doc.mtime,
        summaries if total > 1 else parts,
        partial=total > 1,
    )
    answer = await _stream_answer(deps, messages, [1])
    deps.trace.add_latency("summarize", _ms(t0))
    update.update(answer=answer, citations=sources, target_document_id=doc.id)
    return update


async def save_query_log(
    session: AsyncSession, trace: QueryTrace, message_id: int | None
) -> QueryLog:
    """질의 로그 저장(TRD 5.3). commit은 호출한 쪽이 한다."""
    trace.latency_ms["total"] = trace.elapsed_ms()
    row = QueryLog(
        message_id=message_id,
        rewritten_query=trace.rewritten_query,
        keyword_query=trace.keyword_query,
        filters=trace.filters,
        retrieved=trace.retrieved,
        grade=trace.grade,
        retried=trace.retried,
        not_found=trace.not_found,
        coverage=trace.coverage,
        latency_ms=trace.latency_ms,
        models={**trace.models, "intent": trace.intent},
        source=trace.source,
    )
    session.add(row)
    await session.flush()
    trace.saved = True
    trace.log_id = row.id
    return row


async def log_node(state: QueryState, runtime: Runtime[QueryDeps]) -> dict[str, Any]:
    deps = runtime.context
    await save_query_log(deps.session, deps.trace, state.get("message_id"))
    return {}


# ---------------------------------------------------------------------------
# 그래프
# ---------------------------------------------------------------------------


def _after_scope(state: QueryState) -> str:
    return "log" if state.get("scope_empty") else "rewrite"


def _after_rewrite(state: QueryState) -> str:
    return "summarize" if state.get("intent") == "summarize" else "retrieve"


def _after_grade(state: QueryState) -> str:
    if state.get("grade") == "sufficient":
        return "generate"
    if state.get("attempt", 1) < MAX_ATTEMPTS:
        return "retry_rewrite"
    return "not_found"


def build_graph() -> Any:
    g: StateGraph[QueryState, QueryDeps, QueryState, QueryState] = StateGraph(
        QueryState, context_schema=QueryDeps
    )
    nodes: dict[str, Callable[..., Any]] = {
        "scope": scope_node,
        "rewrite": rewrite_node,
        "retrieve": retrieve_node,
        "rerank": rerank_node,
        "grade": grade_node,
        "retry_rewrite": retry_rewrite_node,
        "generate": generate_node,
        "summarize": summarize_node,
        "not_found": not_found_node,
        "log": log_node,
    }
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_edge(START, "scope")
    g.add_conditional_edges("scope", _after_scope, ["rewrite", "log"])
    g.add_conditional_edges("rewrite", _after_rewrite, ["retrieve", "summarize"])
    g.add_edge("retrieve", "rerank")
    g.add_edge("rerank", "grade")
    g.add_conditional_edges("grade", _after_grade, ["generate", "retry_rewrite", "not_found"])
    g.add_edge("retry_rewrite", "retrieve")
    for name in ("generate", "summarize", "not_found"):
        g.add_edge(name, "log")
    g.add_edge("log", END)
    return g.compile()


GRAPH = build_graph()


@dataclass
class QueryResult:
    answer: str
    citations: list[dict[str, Any]]
    not_found: bool
    coverage: dict[str, Any]
    intent: str | None
    cited: list[int]


async def run_query(
    deps: QueryDeps,
    *,
    question: str,
    history: Sequence[ChatMessage] = (),
    previous_document_ids: Sequence[int] = (),
    filters: SearchFilters | None = None,
    summarize_document_id: int | None = None,
    message_id: int | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """그래프를 실행하며 `(이벤트 이름, 데이터)`를 흘려보낸다.

    `status`·`sources`·`token`을 차례로 내보내고, 마지막에 `("result", QueryResult)`를 낸다.
    """
    deps.trace.models = {
        "llm": deps.llm.model,
        "embed": deps.embedder.model if deps.embedder else None,
        "rerank": deps.reranker.model if deps.reranker else None,
    }
    inputs: QueryState = {
        "question": question,
        "history": list(history),
        "previous_document_ids": list(previous_document_ids),
        "filters": filters or SearchFilters(),
        "summarize_document_id": summarize_document_id,
        "message_id": message_id,
    }
    final: dict[str, Any] = {}
    async for mode, chunk in GRAPH.astream(inputs, context=deps, stream_mode=["custom", "values"]):
        if mode == "custom":
            yield chunk["event"], chunk["data"]
        else:
            final = chunk
    answer = str(final.get("answer") or "")
    yield (
        "result",
        QueryResult(
            answer=answer,
            citations=list(final.get("citations") or []),
            not_found=bool(final.get("not_found")),
            coverage=deps.trace.coverage,
            intent=deps.trace.intent,
            cited=cited_numbers(answer),
        ),
    )
