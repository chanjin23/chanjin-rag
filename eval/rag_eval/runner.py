"""평가 실행 (F-13, PRD 8장, TRD 5.3, 14장).

1. 판정 모델이 답변 모델과 같으면 거절한다.
2. 라벨 경로가 `documents`에 없는 문항은 경고(`eval.item.excluded`)를 남기고 지표에서 뺀다.
3. `eval_runs`에 실행을 만들고(`eval.run.started`), 문항마다 질의 그래프(채팅과 같은 경로)를
   돌린다. 질의 로그는 `source='eval'`로 남는다. 질문 임베딩은 원래 한도를 쓰지 않는다.
4. 최종 순위(마지막 검색의 재정렬 순서)의 청크 `(경로, 위치)`로 Recall@5·MRR을, judge로 정확도·
   충실성을 계산해 `eval_results`에 저장하고, 실행 지표를 `eval_runs.metrics`에 저장한다
   (`eval.run.completed`).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Chunk, Document, EvalResult, EvalRun, QueryLog
from app.providers.base import EmbeddingProvider, LLMProvider, RerankProvider
from app.query.graph import TOP_K, QueryDeps, QueryResult, QueryTrace, run_query
from rag_eval.dataset import Dataset, EvalItem
from rag_eval.judge import Judge, ensure_distinct_models
from rag_eval.metrics import (
    ItemOutcome,
    LabelWarning,
    RetrievedChunk,
    answer_score,
    exclude_missing_labels,
    recall_at_k,
    reciprocal_rank,
    summarize,
)

log = get_logger(__name__)

# eval_results.retrieved에 남기는 순위 수
SAVED_RANKS = 10


@dataclass
class EvalProviders:
    llm: LLMProvider
    embedder: EmbeddingProvider | None
    reranker: RerankProvider | None
    judge: LLMProvider


@dataclass
class EvalReport:
    run: EvalRun
    warnings: list[LabelWarning] = field(default_factory=list)
    outcomes: list[ItemOutcome] = field(default_factory=list)


def ranked_entries(trace: QueryTrace) -> list[dict[str, Any]]:
    """질의 로그 `retrieved` → 마지막 검색 시도의 최종 순서.

    재정렬 점수 내림차순, 재정렬이 없으면 RRF 순위.
    """
    if not trace.retrieved:
        return []
    last = max(int(e.get("attempt", 1)) for e in trace.retrieved)
    entries = [e for e in trace.retrieved if int(e.get("attempt", 1)) == last]
    return sorted(
        entries,
        key=lambda e: (-(e["rerank"]) if e.get("rerank") is not None else 0.0, e["rank"]),
    )


async def _retrieved_chunks(
    session: AsyncSession, entries: Sequence[dict[str, Any]]
) -> list[RetrievedChunk]:
    ids = [int(e["chunk_id"]) for e in entries]
    if not ids:
        return []
    rows = (
        await session.execute(
            select(Chunk.id, Document.path, Chunk.location)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.id.in_(ids))
        )
    ).all()
    found = {r.id: (r.path, dict(r.location or {})) for r in rows}
    out: list[RetrievedChunk] = []
    for chunk_id in ids:
        if chunk_id in found:
            path, location = found[chunk_id]
            out.append(RetrievedChunk(len(out) + 1, chunk_id, path, location))
    return out


async def _cited_materials(session: AsyncSession, result: QueryResult) -> list[str]:
    cited = set(result.cited)
    ids = [
        int(c["chunk_id"])
        for c in result.citations
        if c.get("chunk_id") is not None and c.get("n") is not None and c["n"] in cited
    ]
    if not ids:
        return []
    rows = (await session.execute(select(Chunk.id, Chunk.content).where(Chunk.id.in_(ids)))).all()
    contents = {r.id: r.content for r in rows}
    return [contents[i] for i in ids if i in contents]


async def _known_paths(session: AsyncSession) -> list[str]:
    return list((await session.execute(select(Document.path))).scalars())


async def evaluate_item(
    session: AsyncSession, item: EvalItem, providers: EvalProviders, judge: Judge
) -> tuple[ItemOutcome, QueryResult | None, list[RetrievedChunk], QueryTrace]:
    outcome = ItemOutcome(item.id, item.type, item.expects_not_found)
    trace = QueryTrace(source="eval")
    deps = QueryDeps(
        session=session,
        llm=providers.llm,
        embedder=providers.embedder,
        reranker=providers.reranker,
        trace=trace,
    )
    result: QueryResult | None = None
    async for name, data in run_query(deps, question=item.question):
        if name == "result":
            result = data
    assert result is not None
    retrieved = await _retrieved_chunks(session, ranked_entries(trace))
    outcome.predicted_not_found = result.not_found
    outcome.latency_ms = trace.latency_ms.get("total")
    if not item.expects_not_found:
        outcome.recall_at_5 = recall_at_k(item.evidence, retrieved)
        outcome.reciprocal_rank = reciprocal_rank(item.evidence, retrieved)
        verdict = await judge.accuracy(item.question, item.answer or "", result.answer)
        outcome.verdict = verdict
        outcome.answer_score = answer_score(verdict) if verdict is not None else None
    if not result.not_found:
        materials = await _cited_materials(session, result)
        outcome.faithfulness = await judge.faithfulness(result.answer, materials)
    return outcome, result, retrieved, trace


async def _p95_source(session: AsyncSession, log_ids: Sequence[int]) -> list[float]:
    if not log_ids:
        return []
    total = QueryLog.latency_ms["total"].as_float()
    rows = await session.execute(
        select(total).where(QueryLog.id.in_(log_ids), QueryLog.source == "eval")
    )
    return [float(v) for v in rows.scalars() if v is not None]


async def run_eval(
    session: AsyncSession,
    dataset: Dataset,
    providers: EvalProviders,
    *,
    git_sha: str | None = None,
    dataset_path: str | None = None,
    echo: Callable[[str], None] = lambda _: None,
) -> EvalReport:
    ensure_distinct_models(providers.llm.model, providers.judge.model)
    judge = Judge(providers.judge)

    kept, warnings = exclude_missing_labels(dataset.items, await _known_paths(session))
    for w in warnings:
        log.warning("eval.item.excluded", item_id=w.item_id, missing_paths=len(w.missing_paths))
        echo(f"경고: {w.message}")

    config = {
        "dataset": dataset_path or dataset.name,
        "llm_model": providers.llm.model,
        "embed_model": providers.embedder.model if providers.embedder else None,
        "rerank_model": providers.reranker.model if providers.reranker else None,
        "judge_model": providers.judge.model,
        "top_k": TOP_K,
        "embed_quota": "not_counted",
        "excluded_items": [w.item_id for w in warnings],
    }
    run = EvalRun(dataset_version=dataset.label, git_sha=git_sha, config=config, metrics={})
    session.add(run)
    await session.flush()
    for w in warnings:
        session.add(
            EvalResult(
                run_id=run.id,
                item_id=w.item_id,
                retrieved=[],
                answer=None,
                scores={"excluded": True, "missing_paths": list(w.missing_paths)},
            )
        )
    await session.commit()
    log.info(
        "eval.run.started",
        run_id=run.id,
        dataset_version=run.dataset_version,
        items=len(dataset.items),
        excluded=len(warnings),
        llm_model=providers.llm.model,
        judge_model=providers.judge.model,
    )
    echo(f"평가 실행 #{run.id} 시작: 문항 {len(kept)}개(제외 {len(warnings)}개)")

    run_id = run.id
    report = EvalReport(run=run, warnings=warnings)
    log_ids: list[int] = []
    for item in kept:
        try:
            outcome, result, retrieved, trace = await evaluate_item(session, item, providers, judge)
        except Exception as e:  # noqa: BLE001 - 한 문항의 실패로 전체 실행을 멈추지 않는다
            await session.rollback()
            error = type(e).__name__
            outcome = ItemOutcome(item.id, item.type, item.expects_not_found, error=error)
            result, retrieved, trace = None, [], QueryTrace(source="eval")
            log.warning("eval.item.failed", run_id=run_id, item_id=item.id, error=error)
        if trace.log_id is not None:
            log_ids.append(trace.log_id)
        scores = {**outcome.scores(), "query_log_id": trace.log_id}
        session.add(
            EvalResult(
                run_id=run_id,
                item_id=item.id,
                retrieved=[c.as_dict() for c in retrieved[:SAVED_RANKS]],
                answer=result.answer if result is not None else None,
                scores=scores,
            )
        )
        await session.commit()
        report.outcomes.append(outcome)
        echo(f"  {item.id}: {_line(outcome)}")

    await session.refresh(run)
    run.metrics = summarize(
        report.outcomes,
        excluded=len(warnings),
        latencies=await _p95_source(session, log_ids),
    )
    run.finished_at = await session.scalar(select(func.now()))
    await session.commit()
    log.info(
        "eval.run.completed",
        run_id=run.id,
        **{k: v for k, v in run.metrics.items() if isinstance(v, int | float) or v is None},
    )
    return report


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _line(o: ItemOutcome) -> str:
    if o.error:
        return f"오류 {o.error}"
    if o.expects_not_found:
        return "찾지 못함 맞음" if o.predicted_not_found else "찾지 못함 틀림(답을 함)"
    return (
        f"recall@5 {_fmt(o.recall_at_5)}, rr {_fmt(o.reciprocal_rank)}, "
        f"판정 {o.verdict or '-'}, 충실성 {_fmt(o.faithfulness)}"
    )
