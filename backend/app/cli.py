"""백엔드 관리 CLI. create-user, create-token, reindex는 DB를 붙일 때 추가한다."""

import typer

from app.core.config import get_settings

app = typer.Typer(no_args_is_help=True)


@app.command()
def show_config() -> None:
    """현재 설정을 출력한다(비밀값은 가림)."""
    for key, value in get_settings().model_dump().items():
        typer.echo(f"{key} = {value}")


@app.command()
def version() -> None:
    typer.echo("chanjin-rag-backend 0.1.0")


if __name__ == "__main__":
    app()
