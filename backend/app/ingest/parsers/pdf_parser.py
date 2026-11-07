"""PDF 파서 (PyMuPDF, TRD 3.4).

- 페이지마다 섹션 1개, 위치는 `{"page": n}`(1부터).
- 텍스트 레이어가 없는 페이지는 "스캔본 의심" 경고(`scanned_page`)를 남기고 건너뛴다(OCR은 후속).
- 암호가 걸린 PDF(열 때 비밀번호가 필요)는 `encrypted`, 열 수 없는 파일은 `corrupted`.
"""

from pathlib import Path
from typing import Any

import pymupdf

from app.db.enums import FailureCode
from app.ingest.parsers.base import (
    WARNING_SCANNED_PAGE,
    ParseFailure,
    ParseOutput,
    ParseWarning,
    Section,
)

# PyMuPDF는 타입 정보가 부분적이라(mypy strict에서 untyped call) Any로 다룬다.
_fitz: Any = pymupdf


class PdfParser:
    extensions: tuple[str, ...] = (".pdf",)

    def parse(self, path: Path) -> ParseOutput:
        try:
            doc = _fitz.open(path, filetype="pdf")
        except (_fitz.FileDataError, _fitz.EmptyFileError, RuntimeError, ValueError) as e:
            raise ParseFailure(FailureCode.CORRUPTED, type(e).__name__) from e

        with doc:
            # 소유자 암호만 있는 PDF(인쇄·복사 제한)는 needs_pass가 False라 그대로 읽힌다.
            if doc.needs_pass and not doc.authenticate(""):
                raise ParseFailure(FailureCode.ENCRYPTED)

            sections: list[Section] = []
            warnings: list[ParseWarning] = []
            for index, page in enumerate(doc, start=1):
                text = str(page.get_text("text", sort=True)).strip()
                if not text:
                    warnings.append(ParseWarning(WARNING_SCANNED_PAGE, {"page": index}))
                    continue
                sections.append(Section(location={"page": index}, text=text))
            return ParseOutput(sections=sections, warnings=warnings, meta={"pages": doc.page_count})
