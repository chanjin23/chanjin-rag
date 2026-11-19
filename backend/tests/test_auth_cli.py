"""인증 CLI: create-user, reset-password, create-token (TRD 5.2, 11장).

CLI는 자기 연결로 commit하므로 테스트 DB에 실제로 남는다. 앞뒤로 인증 테이블을 비운다.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import structlog
from typer.testing import CliRunner

from app.cli import app
from app.core.security import sha256_hex, verify_password

runner = CliRunner()

Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def cli_db(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    """CLI가 테스트 DB를 쓰게 하고, 끝나면 CLI가 남긴 사용자·세션·토큰을 지운다.

    CLI는 로그 출력을 CliRunner의 임시 stdout으로 다시 설정하므로 끝나면 원래 설정으로 되돌린다.
    """
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    saved_logging = structlog.get_config()
    scheme, sep, rest = migrated_db.partition("://")
    with psycopg.connect(f"{scheme.split('+', 1)[0]}{sep}{rest}", autocommit=True) as conn:

        def clean() -> None:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM api_tokens")

        clean()
        try:
            yield conn
        finally:
            clean()
            structlog.configure(**saved_logging)


def _user(conn: Conn, username: str) -> tuple[int, str, int, datetime | None]:
    row = conn.execute(
        "SELECT id, password_hash, failed_logins, locked_until FROM users WHERE username = %s",
        (username,),
    ).fetchone()
    assert row is not None
    return row  # type: ignore[return-value]


def test_create_user_and_reset_password(cli_db: Conn) -> None:
    # 비밀번호는 화면에 보이지 않게 두 번 묻는다
    res = runner.invoke(app, ["create-user", "owner"], input="first-password\nfirst-password\n")
    assert res.exit_code == 0, res.output
    assert "first-password" not in res.output
    user_id, pw_hash, _, _ = _user(cli_db, "owner")
    assert pw_hash.startswith("$argon2id$")
    assert verify_password(pw_hash, "first-password")

    # 단일 계정: 두 번째 사용자는 만들 수 없다
    res = runner.invoke(app, ["create-user", "other"], input="other-password\nother-password\n")
    assert res.exit_code == 1
    assert "이미 사용자가 있습니다" in res.output
    assert cli_db.execute("SELECT count(*) FROM users").fetchone() == (1,)

    # 짧은 비밀번호는 거절
    res = runner.invoke(app, ["reset-password", "owner", "--password-stdin"], input="short\n")
    assert res.exit_code == 1
    assert "8자 이상" in res.output

    # 잠기고 세션이 있는 상태에서 비밀번호 재설정 → 잠금 해제, 세션 모두 끊김
    now = datetime.now(UTC)
    cli_db.execute(
        "UPDATE users SET failed_logins = 3, locked_until = %s WHERE id = %s",
        (now + timedelta(minutes=10), user_id),
    )
    cli_db.execute(
        "INSERT INTO sessions (id_hash, user_id, expires_at, last_seen_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (sha256_hex("old-cookie"), user_id, now + timedelta(days=14), now, now),
    )
    res = runner.invoke(
        app, ["reset-password", "owner", "--password-stdin"], input="second-password\n"
    )
    assert res.exit_code == 0, res.output
    _, pw_hash, failed, locked_until = _user(cli_db, "owner")
    assert verify_password(pw_hash, "second-password")
    assert not verify_password(pw_hash, "first-password")
    assert failed == 0
    assert locked_until is None
    assert cli_db.execute("SELECT count(*) FROM sessions").fetchone() == (0,)

    # 없는 사용자
    res = runner.invoke(app, ["reset-password", "ghost", "--password-stdin"], input="whatever-pw\n")
    assert res.exit_code == 1
    assert "사용자가 없습니다" in res.output


def test_create_token_prints_plaintext_once(cli_db: Conn) -> None:
    res = runner.invoke(app, ["create-token", "집 PC"])
    assert res.exit_code == 0, res.output
    raw = res.output.strip().splitlines()[-1]
    assert len(raw) >= 43

    rows = cli_db.execute("SELECT name, token_hash, revoked_at FROM api_tokens").fetchall()
    assert rows == [("집 PC", sha256_hex(raw), None)]
    assert raw not in rows[0][1]
