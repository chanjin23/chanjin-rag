"""문서 파서 (TRD 3.4, 11장).

픽스처 문서는 모두 라이브러리로 `tmp_path`에 만든다(바이너리 커밋 없음).
"""

import subprocess
import textwrap
import time
from pathlib import Path

import docx
import pptx
import pymupdf
import pytest
from pptx.util import Inches
from structlog.testing import capture_logs

from app.db.enums import FailureCode
from app.ingest.parsers import (
    DEFERRED_EXTENSIONS,
    WARNING_SCANNED_PAGE,
    ParseResult,
    get_parser,
    parse_document,
)
from app.ingest.parsers.isolation import parse_in_subprocess
from app.ingest.parsers.office import ENCRYPTED_PACKAGE, OLE_SIGNATURE
from app.ingest.parsers.pptx_parser import NOTES_LABEL

# ---------------------------------------------------------------------------
# 합성 문서 만들기
# ---------------------------------------------------------------------------


def make_pdf(path: Path, pages: list[str | None]) -> Path:
    """페이지마다 글자를 넣는다. None이면 텍스트 레이어 없이 도형만 그린 페이지(스캔본 흉내)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text is None:
            page.draw_rect(pymupdf.Rect(50, 50, 300, 300), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
        else:
            page.insert_text((72, 72), text, fontname="korea")
    doc.save(path)
    doc.close()
    return path


def make_docx(path: Path) -> Path:
    d = docx.Document()
    d.add_heading("보고서", level=0)  # Title
    d.add_paragraph("머리말 문단")
    d.add_heading("1장 개요", level=1)
    d.add_paragraph("개요 첫 문단")
    d.add_paragraph("")  # 빈 문단은 건너뜀
    d.add_heading("1.1 배경", level=2)
    d.add_paragraph("배경 문단")
    table = d.add_table(rows=3, cols=3)
    for r, row in enumerate(
        [["이름", "부서", "비고"], ["홍길동", "개발", ""], ["김철수", "기획", "팀장"]]
    ):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    d.add_heading("2장 결론", level=1)
    d.add_paragraph("결론 문단")
    d.save(str(path))
    return path


def make_pptx(path: Path) -> Path:
    prs = pptx.Presentation()
    # 1: 제목 + 본문 + 표 + 노트. 본문 도형을 표보다 아래에 두어도 순서는 제목→본문→표→노트
    s1 = prs.slides.add_slide(prs.slide_layouts[5])  # Title Only
    s1.shapes.title.text = "분기 실적"
    table = s1.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    for r, row in enumerate([["항목", "값"], ["매출", "100억"]]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    box = s1.shapes.add_textbox(Inches(1), Inches(5), Inches(4), Inches(1))
    box.text_frame.text = "본문 요약"
    s1.notes_slide.notes_text_frame.text = "발표 때 강조할 점"
    # 2: 제목 + 본문 placeholder
    s2 = prs.slides.add_slide(prs.slide_layouts[1])  # Title and Content
    s2.shapes.title.text = "다음 계획"
    s2.placeholders[1].text_frame.text = "채용 확대"
    prs.save(str(path))
    return path


# ---------------------------------------------------------------------------
# 필수 테스트
# ---------------------------------------------------------------------------


def test_pdf_pages_and_scanned_page_warning(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "a.pdf", ["첫 페이지 내용", None, "세 번째 페이지 내용"])
    result = parse_document(pdf)

    assert result.status == "ok"
    assert [s.location for s in result.sections] == [{"page": 1}, {"page": 3}]
    assert result.sections[0].text == "첫 페이지 내용"
    assert result.sections[1].text == "세 번째 페이지 내용"
    assert all(s.kind == "text" for s in result.sections)
    # 텍스트 레이어가 없는 2쪽은 건너뛰고 스캔본 의심 경고를 남긴다
    assert [(w.code, w.location) for w in result.warnings] == [(WARNING_SCANNED_PAGE, {"page": 2})]

    # 하위 프로세스에서도 같은 결과(직렬화 왕복)
    isolated = parse_in_subprocess(pdf)
    assert isolated.sections == result.sections
    assert isolated.warnings == result.warnings


def test_docx_heading_path_and_tables(tmp_path: Path) -> None:
    result = parse_document(make_docx(tmp_path / "a.docx"))
    assert result.status == "ok"

    texts = [(s.kind, s.heading_path, s.text) for s in result.sections]
    assert texts == [
        ("text", ["보고서"], "머리말 문단"),
        ("text", ["보고서", "1장 개요"], "개요 첫 문단"),
        ("text", ["보고서", "1장 개요", "1.1 배경"], "배경 문단"),
        # 표는 행 단위, 첫 행이 헤더. 빈 값은 빠진다
        ("table", ["보고서", "1장 개요", "1.1 배경"], "이름: 홍길동 | 부서: 개발"),
        ("table", ["보고서", "1장 개요", "1.1 배경"], "이름: 김철수 | 부서: 기획 | 비고: 팀장"),
        # 같은 수준의 새 제목이 나오면 아래 계층은 닫힌다
        ("text", ["보고서", "2장 결론"], "결론 문단"),
    ]
    table_locations = [s.location for s in result.sections if s.kind == "table"]
    assert table_locations == [{"table": 1, "row": 2}, {"table": 1, "row": 3}]
    assert result.sections[0].location == {"paragraph": 2}


def test_pptx_slide_order_and_notes(tmp_path: Path) -> None:
    result = parse_document(make_pptx(tmp_path / "a.pptx"))
    assert result.status == "ok"
    assert [s.location for s in result.sections] == [{"slide": 1}, {"slide": 2}]
    assert all(s.kind == "slide" for s in result.sections)

    first = result.sections[0]
    assert first.heading_path == ["분기 실적"]
    lines = first.text.split("\n")
    # 제목 → 본문 → 표 → 발표자 노트 순서
    assert lines == [
        "분기 실적",
        "본문 요약",
        "항목: 매출 | 값: 100억",
        NOTES_LABEL,
        "발표 때 강조할 점",
    ]

    second = result.sections[1]
    assert second.heading_path == ["다음 계획"]
    assert second.text == "다음 계획\n채용 확대"


def test_txt_cp949_detection(tmp_path: Path) -> None:
    body = (
        "한국어 윈도우에서 저장한 메모입니다.\n회의는 다음 주 화요일 오후 세 시에 열립니다.\n"
        "\n"
        "참석자는 개발팀과 기획팀입니다. 안건은 예산 검토와 일정 조정입니다.\n"
    )
    path = tmp_path / "memo.TXT"  # 대문자 확장자도 같은 파서
    path.write_bytes(body.encode("cp949"))

    result = parse_document(path)
    assert result.status == "ok"
    assert result.meta["encoding"] == "cp949"
    assert [s.text for s in result.sections] == [
        "한국어 윈도우에서 저장한 메모입니다.\n회의는 다음 주 화요일 오후 세 시에 열립니다.",
        "참석자는 개발팀과 기획팀입니다. 안건은 예산 검토와 일정 조정입니다.",
    ]
    assert [s.location for s in result.sections] == [{"lines": [1, 2]}, {"lines": [4, 4]}]
    assert all(s.heading_path == [] for s in result.sections)

    # UTF-8(BOM 포함)도 그대로 읽는다
    utf8 = tmp_path / "utf8.txt"
    utf8.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    assert [s.text for s in parse_document(utf8).sections] == [s.text for s in result.sections]


def test_md_heading_path(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        서문 문단

        # 설치
        설치 안내 문단

        ## 윈도우
        윈도우 설치 방법

        ```bash
        # 이 줄은 코드 주석이지 제목이 아니다

        uv sync
        ```

        ### 문제 해결 ###
        자주 묻는 질문

        ## 리눅스
        리눅스 설치 방법

        # 사용법
        사용법 문단
        """
    )
    path = tmp_path / "guide.md"
    path.write_text(md, encoding="utf-8")
    result = parse_document(path)
    assert result.status == "ok"

    got = [(s.heading_path, s.text.split("\n")[0]) for s in result.sections]
    assert got == [
        ([], "서문 문단"),
        (["설치"], "설치 안내 문단"),
        (["설치", "윈도우"], "윈도우 설치 방법"),
        (["설치", "윈도우"], "```bash"),
        (["설치", "윈도우", "문제 해결"], "자주 묻는 질문"),
        (["설치", "리눅스"], "리눅스 설치 방법"),
        (["사용법"], "사용법 문단"),
    ]
    code = result.sections[3]
    assert "# 이 줄은 코드 주석이지 제목이 아니다" in code.text
    assert code.location == {"lines": [9, 13]}
    assert result.sections[0].location == {"lines": [1, 1]}


def test_encrypted_pdf_failure_code(tmp_path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    path = tmp_path / "locked.pdf"
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="user-pw", owner_pw="owner-pw")
    doc.close()

    for result in (parse_document(path), parse_in_subprocess(path)):
        assert result.status == "failed"
        assert result.failure_code == FailureCode.ENCRYPTED
        assert result.sections == []
        assert result.guidance == "암호를 풀고 저장하면 다음 동기화 때 다시 처리됩니다"


def test_corrupted_file_failure_code(tmp_path: Path) -> None:
    garbage = b"this is not a real document \x00\x01\x02" * 50
    files: list[Path] = []
    for name in ("broken.pdf", "broken.docx", "broken.pptx"):
        p = tmp_path / name
        p.write_bytes(garbage)
        files.append(p)
    # ZIP 앞부분만 남은(잘린) DOCX
    good = make_docx(tmp_path / "good.docx").read_bytes()
    truncated = tmp_path / "truncated.docx"
    truncated.write_bytes(good[: len(good) // 2])
    files.append(truncated)
    # 확장자는 DOCX인데 내용은 PPTX(필요한 부분이 없음)
    mismatched = tmp_path / "mismatched.docx"
    mismatched.write_bytes(make_pptx(tmp_path / "x.pptx").read_bytes())
    files.append(mismatched)

    for p in files:
        result = parse_document(p)
        assert result.status == "failed", p.name
        assert result.failure_code == FailureCode.CORRUPTED, (p.name, result.failure_detail)

    isolated = parse_in_subprocess(files[0])
    assert isolated.failure_code == FailureCode.CORRUPTED


def test_empty_document_is_no_text(tmp_path: Path) -> None:
    empty_txt = tmp_path / "empty.txt"
    empty_txt.write_bytes(b"")
    blank_md = tmp_path / "blank.md"
    blank_md.write_text("\n\n   \n", encoding="utf-8")
    empty_docx = tmp_path / "empty.docx"
    docx.Document().save(str(empty_docx))
    empty_pptx = tmp_path / "empty.pptx"
    prs = pptx.Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])  # Blank
    prs.save(str(empty_pptx))
    scanned_pdf = make_pdf(tmp_path / "scanned.pdf", [None, None])

    for p in (empty_txt, blank_md, empty_docx, empty_pptx, scanned_pdf):
        result = parse_document(p)
        # 실패가 아니라 no_text(파일명 청크만 만들 대상)
        assert result.status == "no_text", p.name
        assert result.failure_code is None
        assert result.sections == []

    # 완전 스캔 PDF는 페이지마다 경고가 남는다
    assert len(parse_document(scanned_pdf).warnings) == 2
    assert parse_in_subprocess(empty_docx).status == "no_text"


SLOW_PARSER = """
import time


class SlowParser:
    extensions = (".txt",)

    def parse(self, path):
        time.sleep(60)
"""


def test_parser_timeout_in_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "t04_slow_parser.py").write_text(SLOW_PARSER, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(mod_dir))
    doc = tmp_path / "doc.txt"
    doc.write_text("내용", encoding="utf-8")

    started = time.monotonic()
    with capture_logs() as logs:
        result = parse_in_subprocess(
            doc, timeout=2, parser_ref="t04_slow_parser:SlowParser", document_id=7
        )
    elapsed = time.monotonic() - started

    assert result.status == "failed"
    assert result.failure_code == FailureCode.TIMEOUT
    assert result.guidance == "재처리하거나 파일을 나눠 보세요"
    assert elapsed < 20  # 자식이 60초 잠들어도 타임아웃에 끊는다
    failed = [e for e in logs if e["event"] == "parse.failed"]
    assert len(failed) == 1
    assert failed[0]["failure_code"] == "timeout"
    assert failed[0]["document_id"] == 7
    assert failed[0]["isolated"] is True
    # 파일 경로·이름은 로그에 남기지 않는다
    assert all(str(doc.name) not in str(e) for e in logs)

    # 부모(Worker)는 계속 동작한다: 다음 파일을 정상 파싱
    assert parse_in_subprocess(doc).status == "ok"


def test_deferred_formats_report_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert DEFERRED_EXTENSIONS == {".hwp", ".hwpx", ".xlsx"}

    def no_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("지원하지 않는 형식은 하위 프로세스를 띄우지 않는다")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)

    for name in ("a.hwp", "b.HWPX", "c.xlsx", "d.Xlsx"):
        p = tmp_path / name
        p.write_bytes(b"PK\x03\x04 not parsed")
        assert get_parser(p) is None
        for result in (parse_document(p), parse_in_subprocess(p)):
            assert result.status == "failed"
            assert result.failure_code == FailureCode.UNSUPPORTED_FORMAT
            assert result.sections == []
            assert result.guidance == "이 형식은 아직 지원하지 않습니다. 후속 버전에서 지원합니다"

    with capture_logs() as logs:
        parse_in_subprocess(tmp_path / "a.hwp")
    assert [(e["event"], e["failure_code"], e["isolated"]) for e in logs] == [
        ("parse.failed", "unsupported_format", False)
    ]


# ---------------------------------------------------------------------------
# 보조 테스트
# ---------------------------------------------------------------------------


def test_registry_ignores_extension_case(tmp_path: Path) -> None:
    for ext in (".PDF", ".Docx", ".PPTX", ".Md", ".txt"):
        assert get_parser(tmp_path / f"x{ext}") is not None, ext
    assert get_parser(tmp_path / "x.doc") is None
    pdf = make_pdf(tmp_path / "upper.PDF", ["대문자 확장자"])
    assert parse_document(pdf).status == "ok"


def test_encrypted_office_file_failure_code(tmp_path: Path) -> None:
    # 암호 걸린 OOXML은 OLE 복합 파일 안의 EncryptedPackage 스트림이다(구조만 흉내)
    fake = OLE_SIGNATURE + b"\x00" * 512 + ENCRYPTED_PACKAGE + b"\x00" * 512
    for name in ("locked.docx", "locked.pptx"):
        p = tmp_path / name
        p.write_bytes(fake)
        assert parse_document(p).failure_code == FailureCode.ENCRYPTED


MEMORY_HOG = """
class HogParser:
    extensions = (".txt",)

    def parse(self, path):
        blocks = []
        for _ in range(64):
            blocks.append(bytearray(64 * 1024 * 1024))  # 최대 4GB
        return None
"""


def test_parser_memory_limit_in_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "t04_hog_parser.py").write_text(MEMORY_HOG, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(mod_dir))
    doc = tmp_path / "doc.txt"
    doc.write_text("내용", encoding="utf-8")

    result: ParseResult = parse_in_subprocess(
        doc, timeout=30, memory_limit_mb=512, parser_ref="t04_hog_parser:HogParser"
    )
    assert result.status == "failed"
    assert result.failure_code == FailureCode.MEMORY
