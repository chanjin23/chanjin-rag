"""`rag-agent install --dry-run`과 CLI (TRD 10.1).

실제 작업 스케줄러·레지스트리는 건드리지 않는다.
"""

from pathlib import Path

import pytest
from fakes import TOKEN, FakeSyncServer, write
from typer.testing import CliRunner

from rag_agent import cli
from rag_agent import install as installer

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "agent-home"
    monkeypatch.setenv("RAG_AGENT_HOME", str(home))
    monkeypatch.delenv("RAG_AGENT_SERVER", raising=False)
    monkeypatch.delenv("RAG_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("RAG_AGENT_SCAN_ROOTS", raising=False)
    return home


def _forbid_real_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(plan: object) -> None:
        raise AssertionError("dry-run이 실제 등록을 호출함")

    monkeypatch.setattr(installer, "apply_install", boom)
    monkeypatch.setattr(installer, "apply_uninstall", boom)
    monkeypatch.setattr(installer.subprocess, "run", boom)


def test_install_dry_run_prints_task_and_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_real_changes(monkeypatch)
    monkeypatch.setattr(installer, "pythonw_path", lambda: r"C:\venv\Scripts\pythonw.exe")

    result = runner.invoke(cli.app, ["install", "--dry-run"])

    assert result.exit_code == 0, result.output
    out = result.output
    # 작업 스케줄러: 하루 4회, 놓친 예약 실행, 중복 실행 안 함, 로그온 상태에서만, 콘솔 없이
    assert installer.TASK_NAME in out
    for t in ("09:00", "13:00", "18:00", "22:00"):
        assert f"T{t}:00</StartBoundary>" in out
    assert out.count("<CalendarTrigger>") == 4
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in out
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in out
    assert "<LogonType>InteractiveToken</LogonType>" in out
    assert r"<Command>C:\venv\Scripts\pythonw.exe</Command>" in out
    assert "<Arguments>-m rag_agent sync --source schedule</Arguments>" in out
    # URL 프로토콜 레지스트리
    assert r"HKCU\Software\Classes\chanjin-sync  (기본값) = URL:chanjin-sync Protocol" in out
    assert r"HKCU\Software\Classes\chanjin-sync  URL Protocol = " in out
    assert (
        r"HKCU\Software\Classes\chanjin-sync\shell\open\command  (기본값) = "
        r'"C:\venv\Scripts\pythonw.exe" -m rag_agent protocol "%1"'
    ) in out

    un = runner.invoke(cli.app, ["uninstall", "--dry-run"])
    assert un.exit_code == 0, un.output
    assert installer.TASK_NAME in un.output and r"Software\Classes\chanjin-sync" in un.output


def test_cli_sync_status_and_restore_info(
    root: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    write(root / "a.md", "본문")
    server = FakeSyncServer(root)
    monkeypatch.setattr(cli, "SyncClient", lambda url, token: server.client(token))

    missing = runner.invoke(cli.app, ["sync"])
    assert missing.exit_code == 1 and "login" in missing.output

    monkeypatch.setenv("RAG_AGENT_SERVER", "http://fake.test")
    monkeypatch.setenv("RAG_AGENT_TOKEN", TOKEN)
    done = runner.invoke(cli.app, ["sync"])
    assert done.exit_code == 0, done.output
    assert "동기화 완료: run_id=1" in done.output and "업로드 1" in done.output

    status = runner.invoke(cli.app, ["status"])
    assert status.exit_code == 0, status.output
    assert "마지막 실행: succeeded (run_id=1, 출처 cli" in status.output
    assert "재시도 큐: 0개" in status.output
    assert TOKEN not in status.output

    info = runner.invoke(cli.app, ["restore-info"])
    assert info.exit_code == 0
    assert f"{root}\\backup" in info.output and "pg_restore" in info.output

    log_text = (isolated_home / "agent.log").read_text(encoding="utf-8")
    assert "agent.sync.completed" in log_text and TOKEN not in log_text


def test_cli_login_stores_token_in_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: dict[str, str] = {}
    monkeypatch.setattr(
        cli.credentials.keyring, "set_password", lambda s, u, p: saved.update({f"{s}/{u}": p})
    )
    result = runner.invoke(cli.app, ["login", "--server", "https://rag.test/"], input="tok-1\n")
    assert result.exit_code == 0, result.output
    assert saved == {"chanjin-rag/agent-token": "tok-1"}
    assert "tok-1" not in result.output

    monkeypatch.setattr(cli.credentials.keyring, "get_password", lambda s, u: saved[f"{s}/{u}"])
    status = runner.invoke(cli.app, ["status"])
    assert "서버: https://rag.test" in status.output and "토큰: 저장됨" in status.output
