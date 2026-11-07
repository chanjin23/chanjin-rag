"""`rag-eval` CLI. run, compare는 검색·답변 파이프라인이 생긴 뒤 구현한다."""

import typer

app = typer.Typer(no_args_is_help=True)


@app.command()
def run(dataset: str = "eval/datasets/default.yaml") -> None:
    """평가셋을 실행하고 지표를 저장."""
    typer.echo(f"[rag-eval] '{dataset}' 실행은 아직 구현되지 않았습니다.")
    raise typer.Exit(code=1)


@app.command()
def compare(run_id: int, baseline_id: int) -> None:
    """두 실행의 지표를 비교."""
    typer.echo("[rag-eval] compare는 아직 구현되지 않았습니다.")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
