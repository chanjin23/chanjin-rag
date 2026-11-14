"""`rag-agent` CLI (TRD 10.1).

명령: `sync`(수동 실행, F-03), `status`, `install`, `uninstall`, `login`, `restore-info`,
`protocol`(URL 프로토콜 진입점, TRD 10.2).

환경 변수(개발·테스트·E2E용):
- `RAG_AGENT_HOME`: 상태 폴더(기본 `%LOCALAPPDATA%\\chanjin-rag`)
- `RAG_AGENT_SERVER`, `RAG_AGENT_TOKEN`: 서버 주소·토큰(없으면 `login`으로 저장한 값)
- `RAG_AGENT_BACKUP_DIR`: 백업 내려받을 폴더(기본 `C:\\Chanjin\\dev\\backup`, TRD 10.3)
- `RAG_AGENT_SCAN_ROOTS`(`;` 구분): 서버 루트를 로컬에서 좁힌다(넓힐 수 없음). 스캔하지 않은 서버
  루트는 `scan_errors`로 보내 그 아래 문서가 삭제로 판정되지 않는다.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated

import typer

from rag_agent import credentials, logs
from rag_agent import install as installer
from rag_agent.backup import (
    KEEP_ON_PC,
    WINDOWS_BACKUP_DIR,
    BackupFetcher,
    default_backup_dir,
)
from rag_agent.client import SyncClient
from rag_agent.home import agent_home, log_path, state_db_path
from rag_agent.lock import MUTEX_NAME, NamedMutexLock
from rag_agent.protocol import ProtocolHandler
from rag_agent.state import StateDB
from rag_agent.sync import SyncOutcome, SyncRunner

app = typer.Typer(no_args_is_help=True, add_completion=False)

SOURCES = ("cli", "web", "schedule")


def _open_state() -> tuple[Path, StateDB]:
    home = agent_home()
    return home, StateDB(state_db_path(home))


def _mutex_name(home: Path) -> str:
    """상태 폴더마다 한 인스턴스(기본 폴더는 고정 이름).

    테스트·E2E가 쓰는 다른 상태 폴더는 실제 Agent와 겹치지 않는다.
    """
    if not os.environ.get("RAG_AGENT_HOME"):
        return MUTEX_NAME
    digest = hashlib.sha256(str(home.resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return f"{MUTEX_NAME}-{digest}"


def _limit_roots() -> list[str] | None:
    raw = os.environ.get("RAG_AGENT_SCAN_ROOTS", "")
    roots = [r.strip() for r in raw.split(";") if r.strip()]
    return roots or None


def _run_sync(home: Path, state: StateDB, source: str) -> SyncOutcome | None:
    server = credentials.get_server(state)
    token = credentials.get_token()
    if not server or not token:
        logs.event(logs.get_logger(), "agent.sync.not_configured", source=source)
        return None
    with SyncClient(server, token) as client:
        runner = SyncRunner(
            client,
            state,
            NamedMutexLock(_mutex_name(home)),
            limit_roots=_limit_roots(),
            backups=BackupFetcher(client, default_backup_dir()),
        )
        return runner.run(source)


def _configure_logs(home: Path) -> None:
    token = credentials.get_token()
    logs.configure(log_path(home), secrets=[token] if token else [])


@app.command()
def sync(
    source: Annotated[str, typer.Option(help="실행 출처: cli, web, schedule")] = "cli",
) -> None:
    """수동 동기화 (F-03)."""
    if source not in SOURCES:
        raise typer.BadParameter(f"source는 {', '.join(SOURCES)} 중 하나입니다")
    home, state = _open_state()
    _configure_logs(home)
    try:
        outcome = _run_sync(home, state, source)
    finally:
        state.close()
        logs.close()
    if outcome is None:
        typer.echo("서버 주소나 토큰이 없습니다. `rag-agent login --server <주소>`로 저장하세요.")
        raise typer.Exit(code=1)
    if outcome.status == "already_running":
        typer.echo("이미 다른 동기화가 실행 중이라 종료합니다.")
        return
    s = outcome.stats
    if outcome.status == "failed":
        typer.echo(f"동기화 실패: run_id={outcome.run_id} error={outcome.error}")
        raise typer.Exit(code=1)
    typer.echo(
        f"동기화 완료: run_id={outcome.run_id} 파일 {s.get('files', 0)}개, "
        f"업로드 {s.get('uploaded', 0)}, 메타 등록 {s.get('meta_registered', 0)}, "
        f"업로드 실패 {s.get('upload_failed', 0) + s.get('meta_failed', 0)}, "
        f"사라짐 {s.get('vanished', 0)}, 읽지 못함 {s.get('scan_errors', 0)}, "
        f"해시 재사용 {s.get('hash_reused', 0)}"
    )
    b = outcome.backups
    if b is not None:
        if b.error:
            typer.echo(f"백업 목록을 받지 못했습니다: {b.error}")
        else:
            typer.echo(
                f"백업: 내려받음 {len(b.downloaded)}, 이미 있음 {b.skipped}, "
                f"검증 실패 {len(b.mismatched)}, 실패 {len(b.failed)}, 정리 {len(b.pruned)}"
            )
    skipped = s.get("skipped_formats") or {}
    if skipped:
        typer.echo("건너뛴 형식: " + ", ".join(f"{k} {v}" for k, v in skipped.items()))


@app.command()
def status() -> None:
    """마지막 실행 결과와 재시도 큐 상태."""
    home, state = _open_state()
    try:
        server = credentials.get_server(state) or "(없음)"
        token = "저장됨" if credentials.get_token() else "없음"
        last = state.get("last_run")
        retry = state.retry_items()
        files = state.file_count()
    finally:
        state.close()
    typer.echo(f"상태 폴더: {home}")
    typer.echo(f"서버: {server}")
    typer.echo(f"토큰: {token}")
    typer.echo(f"해시 캐시: {files}개 파일")
    typer.echo(f"재시도 큐: {len(retry)}개")
    if last:
        typer.echo(
            f"마지막 실행: {last.get('status')} (run_id={last.get('run_id')}, "
            f"출처 {last.get('source')}, 끝 {last.get('finished_at')})"
        )
        if last.get("error"):
            typer.echo(f"  오류: {last['error']}")
        typer.echo("  통계: " + json.dumps(last.get("stats") or {}, ensure_ascii=False))
    else:
        typer.echo("마지막 실행: 없음")


@app.command()
def login(
    server: Annotated[
        str | None, typer.Option(help="서버 주소(예: https://rag.example.ts.net)")
    ] = None,
) -> None:
    """Agent 토큰을 Windows 자격 증명 관리자(keyring)에 저장."""
    home, state = _open_state()
    try:
        if server:
            credentials.set_server(state, server)
        token = typer.prompt("Agent 토큰", hide_input=True).strip()
        if not token:
            typer.echo("토큰이 비어 있습니다.")
            raise typer.Exit(code=1)
        credentials.set_token(token)
    finally:
        state.close()
    _configure_logs(home)
    logs.event(logs.get_logger(), "agent.login.saved", server_set=bool(server))
    logs.close()
    typer.echo("토큰을 저장했습니다.")


@app.command()
def install(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="등록하지 않고 내용만 출력")] = False,
) -> None:
    """작업 스케줄러(하루 4회)와 chanjin-sync:// URL 프로토콜 등록."""
    plan = installer.build_plan()
    if dry_run:
        typer.echo("[dry-run] 아래 내용을 등록합니다(실제로 등록하지 않음).")
        installer.print_plan(plan, typer.echo)
        return
    installer.apply_install(plan)
    typer.echo(f"등록했습니다: 작업 스케줄러 '{plan.task_name}', URL 프로토콜 chanjin-sync")


@app.command()
def uninstall(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="지우지 않고 대상만 출력")] = False,
) -> None:
    """install로 등록한 항목 제거."""
    plan = installer.build_plan()
    if dry_run:
        typer.echo("[dry-run] 아래 항목을 지웁니다(실제로 지우지 않음).")
        typer.echo(f"  작업 스케줄러: {plan.task_name}")
        typer.echo(f"  레지스트리: {installer.REGISTRY_ROOT}\\{installer.PROTOCOL_KEY}")
        return
    installer.apply_uninstall(plan)
    typer.echo("등록을 지웠습니다. 상태 폴더와 토큰은 그대로 둡니다.")


@app.command("restore-info")
def restore_info() -> None:
    """서버 복구 안내(TRD 12.2)."""
    _, state = _open_state()
    try:
        config = state.get("config")
    finally:
        state.close()
    root = config.get("allowed_root") if isinstance(config, dict) else None
    download_dir = str(default_backup_dir())
    if os.environ.get("RAG_AGENT_BACKUP_DIR", "").strip():
        backup_dir = download_dir
    else:
        backup_dir = f"{root}\\backup" if root else WINDOWS_BACKUP_DIR
    lines = [
        "서버 복구 안내 (TRD 12.2)",
        f"1. 백업 파일 위치: {backup_dir} (rag-YYYYMMDD.dump.age, 최근 {KEEP_ON_PC}개)",
        "2. 새 VM에서 Compose로 db만 띄웁니다.",
        "3. 백업 파일을 VM으로 복사합니다(Tailscale scp).",
        "4. age -d -i <개인키> 로 복호화합니다(개인키는 PC와 비밀번호 관리자에 보관).",
        "5. pg_restore로 복원한 뒤 api와 worker를 띄웁니다(deploy/scripts/restore.sh).",
        "6. 다음 `rag-agent sync`에서 백업 이후 바뀐 파일만 다시 올라갑니다.",
    ]
    if download_dir != backup_dir:
        lines.insert(2, f"   (이 PC에서 내려받는 폴더: {download_dir})")
    for line in lines:
        typer.echo(line)


@app.command()
def protocol(url: str) -> None:
    """chanjin-sync:// 링크로 실행될 때의 진입점. `run`과 `reveal`만 처리한다(TRD 10.2)."""
    home, state = _open_state()
    _configure_logs(home)
    try:
        handler = ProtocolHandler(state, run_sync=lambda source: _run_sync(home, state, source))
        handler.handle(url)
    finally:
        state.close()
        logs.close()


if __name__ == "__main__":
    app()
