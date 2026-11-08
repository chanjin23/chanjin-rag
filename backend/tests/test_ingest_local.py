"""개발·E2E용 CLI `rag-backend ingest-local <폴더>` (T06).

Agent 없이 로컬 폴더를 업로드와 같은 경로로 큐에 넣고 처리한다. 범위 검증은 Sync와 같고,
허용 폴더(`SYNC_ALLOWED_ROOT`)를 테스트용 임시 폴더로 바꿔 쓴다. 문서는 모두 합성 파일.
CLI는 자기 연결로 commit하므로 앞뒤로 수집 테이블을 비운다.
"""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
import structlog
from docx import Document as DocxDocument
from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()

Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def cli_db(migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("SYNC_ALLOWED_ROOT", str(root))
    monkeypatch.setenv("UPLOAD_TMP_DIR", str(tmp_path / "uploads"))
    saved_logging = structlog.get_config()
    scheme, sep, rest = migrated_db.partition("://")
    with psycopg.connect(f"{scheme.split('+', 1)[0]}{sep}{rest}", autocommit=True) as conn:

        def clean() -> None:
            conn.execute("DELETE FROM documents")
            conn.execute("DELETE FROM embed_quota")

        clean()
        try:
            yield conn
        finally:
            clean()
            structlog.configure(**saved_logging)


def _write_docx(path: Path) -> None:
    d = DocxDocument()
    d.add_heading("분기 보고", level=1)
    d.add_paragraph("3분기 매출은 전년 대비 12% 늘었다.")
    d.save(str(path))


def _make_folder(root: Path) -> Path:
    folder = root / "notes"
    (folder / "sub").mkdir(parents=True)
    (folder / "회의록.md").write_text("# 회의\n\n다음 배포는 금요일에 한다.\n", encoding="utf-8")
    (folder / "sub" / "메모.txt").write_text("우유와 빵을 산다.\n", encoding="utf-8")
    _write_docx(folder / "보고서.docx")
    (folder / "빈파일.txt").write_text("   \n\n", encoding="utf-8")
    (folder / "매출.xlsx").write_bytes(b"PK\x03\x04 not parsed yet")
    # 건너뛰는 것들: 지원하지 않는 확장자, 제외 패턴(~$*, *.tmp, node_modules/)
    (folder / "사진.png").write_bytes(b"\x89PNG")
    (folder / "~$보고서.docx").write_bytes(b"lock")
    (folder / "작업중.tmp").write_text("임시", encoding="utf-8")
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "readme.md").write_text("# 패키지\n\n설명", encoding="utf-8")
    return folder


def _statuses(conn: Conn) -> dict[str, int]:
    rows = conn.execute("SELECT status, count(*) FROM documents GROUP BY status").fetchall()
    return {str(s): int(str(n)) for s, n in rows}


def test_ingest_local_processes_folder(cli_db: Conn, tmp_path: Path) -> None:
    root = tmp_path / "root"
    folder = _make_folder(root)

    res = runner.invoke(app, ["ingest-local", str(folder)])
    assert res.exit_code == 0, res.output
    assert "접수: 새 버전 5, 변경 없음 0, 파일명만 0, 건너뜀 4" in res.output

    # md·txt·docx는 임베딩까지 끝나 ready, 빈 파일은 no_text, xlsx는 unsupported_format(파일명 청크)
    assert _statuses(cli_db) == {"ready": 3, "no_text": 1, "failed": 1}
    failed = cli_db.execute(
        "SELECT d.failure_code, c.content FROM documents d JOIN chunks c ON c.document_id = d.id "
        "WHERE d.status = 'failed'"
    ).fetchall()
    assert len(failed) == 1
    assert failed[0][0] == "unsupported_format" and str(failed[0][1]).startswith("매출.xlsx")
    # 경로는 원래 경로 그대로, 파일명 검색용 사본도 채워짐
    paths = {str(p) for (p,) in cli_db.execute("SELECT path FROM documents").fetchall()}
    assert str(folder / "sub" / "메모.txt") in paths
    missing = cli_db.execute(
        "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE d.status = 'ready' AND c.embedding IS NULL"
    ).fetchone()
    assert missing == (0,)
    bm25 = cli_db.execute("SELECT bm25_text FROM chunks WHERE content LIKE '%%매출은%%'").fetchone()
    assert bm25 is not None and "매출" in str(bm25[0])
    # 모든 작업이 끝났고, 임시 원본은 남지 않는다
    assert cli_db.execute("SELECT DISTINCT status FROM jobs").fetchall() == [("done",)]
    assert list((tmp_path / "uploads").iterdir()) == []
    quota = cli_db.execute("SELECT used, chunks_used FROM embed_quota").fetchone()
    assert quota is not None and quota[0] == 3

    # 다시 실행하면 바뀐 파일이 없으므로 아무것도 하지 않는다
    res = runner.invoke(app, ["ingest-local", str(folder)])
    assert res.exit_code == 0, res.output
    assert "접수: 새 버전 0, 변경 없음 5" in res.output

    # 파일 하나를 고치면 그 파일만 다시 처리한다
    (folder / "sub" / "메모.txt").write_text("우유와 빵과 계란을 산다.\n", encoding="utf-8")
    res = runner.invoke(app, ["ingest-local", str(folder)])
    assert res.exit_code == 0, res.output
    assert "접수: 새 버전 1, 변경 없음 4" in res.output
    assert _statuses(cli_db) == {"ready": 3, "no_text": 1, "failed": 1}

    # --no-process: 큐에만 넣는다
    (folder / "새글.md").write_text("# 새 글\n\n큐에만 넣는다.", encoding="utf-8")
    res = runner.invoke(app, ["ingest-local", str(folder), "--no-process"])
    assert res.exit_code == 0, res.output
    assert cli_db.execute(
        "SELECT count(*) FROM jobs WHERE status = 'queued' AND type = 'extract'"
    ).fetchone() == (1,)


def test_ingest_local_rejects_out_of_scope_folder(cli_db: Conn, tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.md").write_text("# 밖\n\n범위 밖", encoding="utf-8")
    fixed = root / "project" / "app"  # 고정 제외(/project/)
    fixed.mkdir(parents=True)
    (fixed / "b.md").write_text("# 제외\n\n고정 제외", encoding="utf-8")

    res = runner.invoke(app, ["ingest-local", str(outside)])
    assert res.exit_code == 1
    assert "outside_root" in res.output
    res = runner.invoke(app, ["ingest-local", str(fixed)])
    assert res.exit_code == 1
    assert "fixed_exclude" in res.output
    # 허용 폴더 전체를 넣어도 고정 제외 아래 파일은 건너뛴다
    res = runner.invoke(app, ["ingest-local", str(root), "--no-process"])
    assert res.exit_code == 0, res.output
    assert "접수: 새 버전 0" in res.output
    assert cli_db.execute("SELECT count(*) FROM documents").fetchone() == (0,)
