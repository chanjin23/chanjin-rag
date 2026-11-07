"""백엔드 관리 CLI(`rag-backend`). create-user, create-token, reindex는 해당 작업에서 추가한다."""

import typer

from app.core.config import get_settings
from app.core.logging import configure_logging

app = typer.Typer(no_args_is_help=True)


def _setup_logging() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json, file=settings.log_file)


@app.command()
def show_config() -> None:
    """현재 설정을 출력한다(비밀값은 가림)."""
    for key, value in get_settings().model_dump().items():
        typer.echo(f"{key} = {value}")


@app.command()
def migrate(
    revision: str = typer.Argument("head", help="올릴 리비전(기본 head)"),
) -> None:
    """DB 스키마를 올린다(`alembic upgrade`). 대상 DB는 `DATABASE_URL`."""
    from app.db import migrate as db_migrate

    _setup_logging()
    db_migrate.upgrade(revision)


@app.command()
def downgrade(
    revision: str = typer.Argument(..., help="내릴 리비전(예: -1, base)"),
) -> None:
    """DB 스키마를 내린다(`alembic downgrade`). `base`는 모든 테이블을 지운다."""
    from app.db import migrate as db_migrate

    _setup_logging()
    db_migrate.downgrade(revision)


@app.command()
def version() -> None:
    typer.echo("chanjin-rag-backend 0.1.0")


if __name__ == "__main__":
    app()
