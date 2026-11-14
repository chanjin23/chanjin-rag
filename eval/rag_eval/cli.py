"""`rag-eval` CLI (F-13, TRD 14장).

- `rag-eval run [--dataset eval/datasets/default.yaml]`: 평가셋을 실행해 지표를 `eval_runs`·
  `eval_results`에 저장하고, 바로 이전 실행과 비교해 출력한다.
- `rag-eval compare <a> <b>`: 실행 a(기준) 대비 b의 지표 증감(b − a)을 출력한다.

모델은 `PROVIDER` 설정을 따른다(테스트·E2E는 fake). 판정 모델은 `EVAL_JUDGE_MODEL`(답변 모델과
달라야 함). DB는 `DATABASE_URL`.
"""

import asyncio
import subprocess
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.eval import METRICS, metric_deltas
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import EvalRun
from rag_eval.dataset import DatasetError, load_dataset
from rag_eval.judge import JudgeConfigError, create_judge_provider
from rag_eval.runner import EvalProviders, EvalReport, run_eval

app = typer.Typer(no_args_is_help=True)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = REPO_ROOT / "eval" / "datasets" / "default.yaml"


class CompareError(LookupError):
    pass


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------


def _value(key: str, value: Any) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "-"
    return f"{value:.0f}" if key == "p95_ms" else f"{value:.3f}"


def _delta(key: str, value: float | None) -> str:
    if value is None:
        return "-"
    text = f"{value:+.0f}" if key == "p95_ms" else f"{value:+.3f}"
    return text


def format_metrics(run: EvalRun) -> list[str]:
    m = run.metrics or {}
    lines = [
        f"실행 #{run.id} ({run.dataset_version}) 판정 모델 {(run.config or {}).get('judge_model')}",
        f"문항 {m.get('items', 0)}개: 평가 {m.get('evaluated', 0)}, 제외 {m.get('excluded', 0)}, "
        f"오류 {m.get('errors', 0)}",
    ]
    for key, label, target, higher in METRICS:
        goal = f"{'≥' if higher else '≤'} {_value(key, target)}"
        lines.append(f"  {label:<20} {_value(key, m.get(key)):>10}   목표 {goal}")
    return lines


def format_comparison(base: EvalRun, target: EvalRun) -> list[str]:
    deltas = metric_deltas(target.metrics or {}, base.metrics or {})
    lines = [
        f"비교: #{base.id} ({base.dataset_version}) → #{target.id} ({target.dataset_version})",
        f"  {'지표':<20} {'#' + str(base.id):>10} {'#' + str(target.id):>10} {'증감':>10}",
    ]
    for key, label, _target, _higher in METRICS:
        lines.append(
            f"  {label:<20} {_value(key, (base.metrics or {}).get(key)):>10} "
            f"{_value(key, (target.metrics or {}).get(key)):>10} {_delta(key, deltas[key]):>10}"
        )
    return lines


# ---------------------------------------------------------------------------
# 명령 본체(테스트가 테스트 DB 세션과 Fake Provider로 직접 부른다)
# ---------------------------------------------------------------------------


async def run_dataset(
    session: AsyncSession,
    dataset_path: Path,
    providers: EvalProviders,
    *,
    git_sha: str | None = None,
    echo: Callable[[str], None] = typer.echo,
) -> EvalReport:
    dataset = load_dataset(dataset_path)
    report = await run_eval(
        session,
        dataset,
        providers,
        git_sha=git_sha,
        dataset_path=str(dataset_path),
        echo=echo,
    )
    for line in format_metrics(report.run):
        echo(line)
    previous = await session.scalar(
        select(EvalRun)
        .where(EvalRun.id < report.run.id, EvalRun.finished_at.is_not(None))
        .order_by(EvalRun.id.desc())
        .limit(1)
    )
    if previous is not None:
        for line in format_comparison(previous, report.run):
            echo(line)
    return report


async def compare_runs(session: AsyncSession, base_id: int, target_id: int) -> list[str]:
    runs = {
        r.id: r
        for r in (
            await session.execute(select(EvalRun).where(EvalRun.id.in_([base_id, target_id])))
        ).scalars()
    }
    missing = [i for i in (base_id, target_id) if i not in runs]
    if missing:
        raise CompareError(f"평가 실행을 찾을 수 없습니다: {', '.join(f'#{i}' for i in missing)}")
    return format_comparison(runs[base_id], runs[target_id])


# ---------------------------------------------------------------------------
# typer 명령
# ---------------------------------------------------------------------------


def _run_async[T](main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    if sys.platform == "win32":
        return asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(main())


def _setup_logging() -> None:
    s = get_settings()
    configure_logging(s.log_level, json=s.log_json, file=s.log_file)


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return None
    return out.stdout.strip() or None


async def _close(provider: object) -> None:
    aclose = getattr(provider, "aclose", None)
    if aclose is not None:
        await aclose()


def _fail(message: str) -> None:
    typer.echo(f"오류: {message}", err=True)
    raise typer.Exit(1)


@app.command()
def run(
    dataset: Annotated[Path, typer.Option("--dataset", "-d", help="평가셋 YAML")] = DEFAULT_DATASET,
) -> None:
    """평가셋을 실행하고 지표를 저장한다. 이전 실행과의 증감도 출력한다."""
    from app.db.engine import create_engine, create_sessionmaker
    from app.providers.factory import (
        create_embedding_provider,
        create_llm_provider,
        create_rerank_provider,
    )

    _setup_logging()

    async def main() -> None:
        providers = EvalProviders(
            llm=create_llm_provider(),
            embedder=create_embedding_provider(),
            reranker=create_rerank_provider(),
            judge=create_judge_provider(),
        )
        engine = create_engine()
        try:
            async with create_sessionmaker(engine)() as session:
                await run_dataset(session, dataset, providers, git_sha=_git_sha())
        finally:
            for p in (providers.llm, providers.embedder, providers.reranker, providers.judge):
                await _close(p)
            await engine.dispose()

    try:
        _run_async(main)
    except (DatasetError, JudgeConfigError, FileNotFoundError) as e:
        _fail(str(e))


@app.command()
def compare(
    a: Annotated[int, typer.Argument(help="기준 실행 ID")],
    b: Annotated[int, typer.Argument(help="비교할 실행 ID")],
) -> None:
    """실행 a 대비 b의 지표 증감(b − a)을 출력한다."""
    from app.db.engine import create_engine, create_sessionmaker

    _setup_logging()

    async def main() -> list[str]:
        engine = create_engine()
        try:
            async with create_sessionmaker(engine)() as session:
                return await compare_runs(session, a, b)
        finally:
            await engine.dispose()

    try:
        lines = _run_async(main)
    except CompareError as e:
        _fail(str(e))
        return
    for line in lines:
        typer.echo(line)


if __name__ == "__main__":
    app()
