"""DOCX 파서 (python-docx, TRD 3.4).

- 본문을 문서 순서대로 읽는다(문단과 표가 섞인 순서 유지).
- 제목 계층: 문단 스타일 `Heading n`/`제목 n`(또는 스타일·문단의 개요 수준)으로
  `heading_path`를 만든다. `Title` 스타일은 가장 위(0수준) 제목으로 본다.
  제목 문단 자체는 섹션으로 만들지 않고 경로로만 쓴다.
- 일반 문단: 문단 1개 = 섹션 1개, 위치 `{"paragraph": n}`(본문 문단 순번, 1부터, 제목 포함).
- 표: 행 단위 `kind=table` 섹션. 첫 행을 헤더로 보고 데이터 행을 `헤더: 값 | 헤더: 값`으로 만든다.
  위치 `{"table": t, "row": r}`(r은 표 안의 행 번호, 1부터. 헤더가 1행).
  행이 1개뿐이면 그 행을 그대로 쓴다.
"""

import re
import zipfile
from pathlib import Path

import docx
from docx.document import Document as DocxDocument
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml import etree

from app.db.enums import FailureCode
from app.ingest.parsers.base import ParseFailure, ParseOutput, Section
from app.ingest.parsers.office import check_ooxml_container

_HEADING_NAME = re.compile(r"^(?:heading|제목)\s*(\d)$", re.IGNORECASE)
_BODY_OUTLINE_LEVEL = 9  # w:outlineLvl 9 = 본문


def _outline_level(element: BaseOxmlElement) -> int | None:
    # python-docx의 oxml 요소는 `w:` 접두사를 알고 있어 namespaces 인자를 받지 않는다
    values = element.xpath("./w:pPr/w:outlineLvl/@w:val")
    if values:
        try:
            level = int(values[0])
        except ValueError:
            return None
        return None if level >= _BODY_OUTLINE_LEVEL else level + 1
    return None


def heading_level(paragraph: Paragraph) -> int | None:
    """제목 문단이면 수준(Title=0, Heading 1=1 …), 아니면 None."""
    level = _outline_level(paragraph._p)
    if level is not None:
        return level
    style = paragraph.style
    depth = 0
    while style is not None and depth < 10:  # 기반 스타일을 따라 올라간다(사용자 정의 제목 스타일)
        name = (style.name or "").strip()
        if name.lower() == "title":
            return 0
        m = _HEADING_NAME.match(name)
        if m:
            return int(m.group(1))
        level = _outline_level(style.element)
        if level is not None:
            return level
        style = style.base_style
        depth += 1
    return None


def _cell_texts(row_cells: list[object]) -> list[str]:
    """병합된 셀은 같은 셀 객체가 반복되므로 연속 중복을 뺀다."""
    texts: list[str] = []
    prev: object | None = None
    for cell in row_cells:
        tc = getattr(cell, "_tc", None)
        if tc is not None and tc is prev:
            continue
        prev = tc
        text = str(getattr(cell, "text", ""))
        texts.append(" ".join(text.split()))
    return texts


def table_row_text(header: list[str], cells: list[str]) -> str:
    """`헤더: 값 | 헤더: 값`. 빈 값은 빼고, 헤더가 빈 열은 `열n`으로 부른다."""
    parts: list[str] = []
    for i, value in enumerate(cells):
        if not value:
            continue
        name = header[i] if i < len(header) and header[i] else f"열{i + 1}"
        parts.append(f"{name}: {value}")
    return " | ".join(parts)


def table_sections(
    rows: list[list[str]], *, table_no: int, heading_path: list[str], key: str = "table"
) -> list[Section]:
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    if len(rows) == 1:
        text = " | ".join(c for c in rows[0] if c)
        return [Section({key: table_no, "row": 1}, text, "table", list(heading_path))]
    header = rows[0]
    out: list[Section] = []
    for r, cells in enumerate(rows[1:], start=2):
        text = table_row_text(header, cells)
        if text:
            out.append(Section({key: table_no, "row": r}, text, "table", list(heading_path)))
    return out


class DocxParser:
    extensions: tuple[str, ...] = (".docx",)

    def parse(self, path: Path) -> ParseOutput:
        check_ooxml_container(path)
        try:
            document: DocxDocument = docx.Document(str(path))
        except (
            PackageNotFoundError,
            zipfile.BadZipFile,
            KeyError,
            ValueError,
            etree.XMLSyntaxError,
        ) as e:
            raise ParseFailure(FailureCode.CORRUPTED, type(e).__name__) from e

        sections: list[Section] = []
        stack: list[tuple[int, str]] = []  # (수준, 제목)
        paragraph_no = 0
        table_no = 0
        for block in document.iter_inner_content():
            path_now = [t for _, t in stack]
            if isinstance(block, Paragraph):
                paragraph_no += 1
                text = block.text.strip()
                if not text:
                    continue
                level = heading_level(block)
                if level is not None:
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    stack.append((level, " ".join(text.split())))
                    continue
                sections.append(Section({"paragraph": paragraph_no}, text, "text", path_now))
            elif isinstance(block, Table):
                table_no += 1
                rows = [_cell_texts(list(row.cells)) for row in block.rows]
                sections.extend(table_sections(rows, table_no=table_no, heading_path=path_now))
        return ParseOutput(sections=sections, meta={"tables": table_no})
