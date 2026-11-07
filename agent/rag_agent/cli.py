"""`rag-agent` CLI (TRD 10.1). 지금은 명령 이름만 잡아 두고, 동작은 이후 구현한다."""

import typer

app = typer.Typer(no_args_is_help=True)


def _todo(name: str) -> None:
    typer.echo(f"[rag-agent] '{name}'는 아직 구현되지 않았습니다 (TRD 10장).")
    raise typer.Exit(code=1)


@app.command()
def sync() -> None:
    """수동 동기화 (F-03)."""
    _todo("sync")


@app.command()
def status() -> None:
    """마지막 실행 결과와 재시도 큐 상태."""
    _todo("status")


@app.command()
def login() -> None:
    """Agent 토큰을 Windows 자격 증명 관리자에 저장."""
    _todo("login")


@app.command()
def install() -> None:
    """작업 스케줄러(하루 4회)와 chanjin-sync:// URL 프로토콜 등록."""
    _todo("install")


@app.command()
def uninstall() -> None:
    """install로 등록한 항목 제거."""
    _todo("uninstall")


@app.command()
def protocol(url: str) -> None:
    """chanjin-sync:// 링크로 실행될 때의 진입점. 허용 동작은 run 하나뿐 (TRD 10.2)."""
    _todo("protocol")


if __name__ == "__main__":
    app()
