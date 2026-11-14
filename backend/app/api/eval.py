"""평가 결과 API (F-13, TRD 8.3, 9.1).

- `GET /eval/runs`: 실행 목록(최신순). 실행마다 지표와 바로 이전 실행(id가 더 작은 완료된 실행)
  대비 증감.
- `GET /eval/runs/{id}`: 실행 하나의 설정·지표·증감과 문항별 결과.

실행 자체는 CLI(`rag-eval run`)가 하고 결과를 `eval_runs`·`eval_results`에 저장한다.
지표 정의와 증감 계산(`metric_deltas`)은 CLI의 `rag-eval compare`도 같이 쓴다.
로그: `eval.runs.listed`, `eval.run.read`.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbSession
from app.api.errors import ApiError
from app.core.logging import get_logger
from app.db.models import EvalResult, EvalRun

router = APIRouter(prefix="/eval", tags=["eval"])
log = get_logger(__name__)

# (키, 이름, 목표, 높을수록 좋은가) — PRD 8장 표 순서
METRICS: list[tuple[str, str, float, bool]] = [
    ("recall_at_5", "Recall@5", 0.80, True),
    ("mrr", "MRR", 0.60, True),
    ("faithfulness", "근거 충실성", 0.90, True),
    ("answer_accuracy", "답변 정확도", 0.75, True),
    ("not_found_accuracy", "찾지 못함 정확도", 0.80, True),
    ("p95_ms", "p95 응답 시간(ms)", 8000.0, False),
]
METRIC_KEYS = [m[0] for m in METRICS]


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def metric_deltas(
    current: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, float | None]:
    """지표별 증감(현재 − 이전). 어느 한쪽이 없으면 None."""
    out: dict[str, float | None] = {}
    for key in METRIC_KEYS:
        a, b = _num(current.get(key)), _num((previous or {}).get(key))
        out[key] = None if a is None or b is None else round(a - b, 4)
    return out


class MetricDef(BaseModel):
    key: str
    label: str
    target: float
    higher_is_better: bool


class RunSummary(BaseModel):
    id: int
    dataset_version: str
    git_sha: str | None
    judge_model: str | None
    llm_model: str | None
    started_at: datetime
    finished_at: datetime | None
    metrics: dict[str, Any]
    previous_id: int | None
    previous_metrics: dict[str, Any] | None
    deltas: dict[str, float | None]


class RunList(BaseModel):
    metrics: list[MetricDef]
    items: list[RunSummary]


class ItemResult(BaseModel):
    item_id: str
    answer: str | None
    retrieved: list[dict[str, Any]]
    scores: dict[str, Any]


class RunDetail(RunSummary):
    config: dict[str, Any]
    results: list[ItemResult]


def _metric_defs() -> list[MetricDef]:
    return [MetricDef(key=k, label=lbl, target=t, higher_is_better=h) for k, lbl, t, h in METRICS]


def _summary(run: EvalRun, previous: EvalRun | None) -> RunSummary:
    return RunSummary(
        id=run.id,
        dataset_version=run.dataset_version,
        git_sha=run.git_sha,
        judge_model=(run.config or {}).get("judge_model"),
        llm_model=(run.config or {}).get("llm_model"),
        started_at=run.started_at,
        finished_at=run.finished_at,
        metrics=run.metrics or {},
        previous_id=previous.id if previous else None,
        previous_metrics=(previous.metrics or {}) if previous else None,
        deltas=metric_deltas(run.metrics or {}, previous.metrics if previous else None),
    )


def previous_finished(runs: list[EvalRun], run: EvalRun) -> EvalRun | None:
    """`runs`(id 내림차순) 중 `run`보다 앞서 끝난 실행."""
    return next(
        (r for r in runs if r.id < run.id and r.finished_at is not None),
        None,
    )


@router.get("/runs")
async def list_runs(db: DbSession, limit: int = 50) -> RunList:
    limit = max(1, min(limit, 200))
    runs = list(
        (await db.execute(select(EvalRun).order_by(EvalRun.id.desc()).limit(limit + 20))).scalars()
    )
    items = [_summary(r, previous_finished(runs, r)) for r in runs[:limit]]
    log.info("eval.runs.listed", count=len(items))
    return RunList(metrics=_metric_defs(), items=items)


@router.get("/runs/{run_id}")
async def get_run(run_id: int, db: DbSession) -> RunDetail:
    run = await db.get(EvalRun, run_id)
    if run is None:
        raise ApiError(404, "eval_run_not_found", "평가 실행을 찾을 수 없습니다")
    previous = await db.scalar(
        select(EvalRun)
        .where(EvalRun.id < run_id, EvalRun.finished_at.is_not(None))
        .order_by(EvalRun.id.desc())
        .limit(1)
    )
    results = (
        await db.execute(
            select(EvalResult).where(EvalResult.run_id == run_id).order_by(EvalResult.item_id)
        )
    ).scalars()
    summary = _summary(run, previous)
    log.info("eval.run.read", run_id=run_id, previous_id=summary.previous_id)
    return RunDetail(
        **summary.model_dump(),
        config=run.config or {},
        results=[
            ItemResult(item_id=r.item_id, answer=r.answer, retrieved=r.retrieved, scores=r.scores)
            for r in results
        ],
    )
