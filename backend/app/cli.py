"""백엔드 관리 CLI(`rag-backend`). reindex는 해당 작업에서 추가한다.

인증(TRD 5.2, 11장): `create-user`, `reset-password`, `create-token`.
- 비밀번호는 기본으로 화면에 보이지 않게 두 번 묻는다. 스크립트(E2E 등)는 `--password-stdin`으로
  표준 입력 첫 줄을 넘긴다. 명령줄 인자로는 받지 않는다(셸 기록·프로세스 목록에 남지 않게).
- Agent 토큰 평문은 발급할 때 한 번만 출력한다. DB에는 SHA-256만 저장한다.
"""

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import NoReturn

import typer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

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


def _run_db[T](work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """명령 하나를 DB 트랜잭션 하나로 실행하고 commit한다. 엔진은 명령마다 만들고 닫는다."""
    from app.db.engine import create_engine, create_sessionmaker

    async def main() -> T:
        engine = create_engine()
        try:
            async with create_sessionmaker(engine)() as session:
                result = await work(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    if sys.platform == "win32":
        # psycopg async는 Windows Proactor 루프에서 동작하지 않는다(app.db.engine 참고)
        return asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(main())


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    password: str = typer.prompt("비밀번호", hide_input=True, confirmation_prompt=True)
    return password


def _fail(message: str) -> NoReturn:
    typer.echo(f"오류: {message}", err=True)
    raise typer.Exit(1)


@app.command("create-user")
def create_user(
    username: str = typer.Argument(..., help="로그인 아이디"),
    password_stdin: bool = typer.Option(
        False, "--password-stdin", help="비밀번호를 묻지 않고 표준 입력 첫 줄에서 읽는다"
    ),
) -> None:
    """웹 로그인 사용자를 만든다(단일 계정: 이미 있으면 실패)."""
    from app.core import auth

    _setup_logging()
    password = _read_password(password_stdin)
    try:
        user = _run_db(lambda s: auth.create_user(s, username, password))
    except auth.AuthError as e:
        _fail(str(e))
    get_logger("app.cli").info("auth.user.created", user_id=user.id)
    typer.echo(f"사용자를 만들었습니다: {user.username} (id={user.id})")


@app.command("reset-password")
def reset_password(
    username: str = typer.Argument(..., help="로그인 아이디"),
    password_stdin: bool = typer.Option(
        False, "--password-stdin", help="비밀번호를 묻지 않고 표준 입력 첫 줄에서 읽는다"
    ),
) -> None:
    """비밀번호를 바꾼다. 잠금을 풀고 기존 로그인 세션을 모두 끊는다."""
    from app.core import auth

    _setup_logging()
    password = _read_password(password_stdin)
    try:
        user = _run_db(lambda s: auth.reset_password(s, username, password))
    except auth.AuthError as e:
        _fail(str(e))
    get_logger("app.cli").info("auth.password.reset", user_id=user.id)
    typer.echo(f"비밀번호를 바꾸고 잠금을 풀었습니다: {user.username}")


@app.command("create-token")
def create_token(
    name: str = typer.Argument(..., help="토큰 이름(예: 집 PC)"),
) -> None:
    """Sync Agent용 API 토큰을 발급한다. 평문 토큰은 지금 한 번만 보여준다."""
    from app.core import auth

    _setup_logging()
    token, raw = _run_db(lambda s: auth.create_api_token(s, name))
    get_logger("app.cli").info("auth.token.created", token_id=token.id)
    typer.echo(f"토큰을 발급했습니다: {token.name} (id={token.id})")
    typer.echo("아래 토큰은 다시 볼 수 없습니다. Agent에 바로 등록하세요(`rag-agent login`).")
    typer.echo(raw)


@app.command()
def version() -> None:
    typer.echo("chanjin-rag-backend 0.1.0")


if __name__ == "__main__":
    app()
