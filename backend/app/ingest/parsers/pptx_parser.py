"""PPTX 파서 (python-pptx, TRD 3.4).

- 슬라이드 1장 = 섹션 1개(`kind=slide`), 위치 `{"slide": n}`(1부터), `heading_path`는 슬라이드 제목.
- 텍스트 순서: 제목 → 본문 도형(위→아래, 왼쪽→오른쪽, 그룹 안까지) → 표 → 발표자 노트.
- 표는 첫 행을 헤더로 보고 `헤더: 값 | 헤더: 값` 줄로 만든다(DOCX 표와 같은 형식).
- 발표자 노트는 `발표자 노트:` 줄 뒤에 붙인다.
"""

import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pptx
from lxml import etree
from pptx.exc import PackageNotFoundError
from pptx.shapes.base import BaseShape
from pptx.shapes.group import GroupShape

from app.db.enums import FailureCode
from app.ingest.parsers.base import ParseFailure, ParseOutput, Section
from app.ingest.parsers.docx_parser import table_sections
from app.ingest.parsers.office import check_ooxml_container

NOTES_LABEL = "발표자 노트:"


def _iter_shapes(shapes: Any) -> Iterator[BaseShape]:
    """그룹을 펼쳐 읽는 순서(위→아래, 왼쪽→오른쪽)로 돌려준다."""
    ordered = sorted(shapes, key=lambda s: (s.top or 0, s.left or 0))
    for shape in ordered:
        if isinstance(shape, GroupShape):
            yield from _iter_shapes(shape.shapes)
        else:
            yield shape


def _text_of(shape: BaseShape) -> str:
    if not getattr(shape, "has_text_frame", False):
        return ""
    lines = [p.text.strip() for p in shape.text_frame.paragraphs]  # type: ignore[attr-defined]
    return "\n".join(line for line in lines if line)


def _table_lines(shape: BaseShape, table_no: int) -> list[str]:
    rows: list[list[str]] = []
    for row in shape.table.rows:  # type: ignore[attr-defined]
        rows.append([" ".join(cell.text.split()) for cell in row.cells])
    return [s.text for s in table_sections(rows, table_no=table_no, heading_path=[])]


class PptxParser:
    extensions: tuple[str, ...] = (".pptx",)

    def parse(self, path: Path) -> ParseOutput:
        check_ooxml_container(path)
        try:
            prs = pptx.Presentation(str(path))
        except (
            PackageNotFoundError,
            zipfile.BadZipFile,
            KeyError,
            ValueError,
            etree.XMLSyntaxError,
        ) as e:
            raise ParseFailure(FailureCode.CORRUPTED, type(e).__name__) from e

        sections: list[Section] = []
        for number, slide in enumerate(prs.slides, start=1):
            title_shape = slide.shapes.title
            title = " ".join(_text_of(title_shape).split()) if title_shape is not None else ""
            title_id = title_shape.shape_id if title_shape is not None else None

            body: list[str] = []
            tables: list[str] = []
            table_no = 0
            for shape in _iter_shapes(slide.shapes):
                if title_id is not None and shape.shape_id == title_id:
                    continue
                if getattr(shape, "has_table", False):
                    table_no += 1
                    tables.extend(_table_lines(shape, table_no))
                    continue
                text = _text_of(shape)
                if text:
                    body.append(text)

            notes = ""
            if slide.has_notes_slide:
                frame = slide.notes_slide.notes_text_frame
                if frame is not None:
                    notes = frame.text.strip()

            parts = [p for p in (title, *body, *tables) if p]
            if notes:
                parts.append(f"{NOTES_LABEL}\n{notes}")
            sections.append(
                Section(
                    location={"slide": number},
                    text="\n".join(parts),
                    kind="slide",
                    heading_path=[title] if title else [],
                )
            )
        return ParseOutput(sections=sections, meta={"slides": len(prs.slides)})
