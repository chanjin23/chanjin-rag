"""문서 파서 (TRD 3.4, 11장).

- `isolation.parse_in_subprocess(path)`: Worker가 쓰는 진입점. 하위 프로세스에서
  타임아웃·메모리 상한과 함께 파싱. 자식 프로세스 시작이 느려지지 않게(설정·로그 모듈을
  읽지 않게) 이 패키지 `__init__`에서는 import하지 않는다.
- `parse_document(path)`: 같은 프로세스에서 파싱(테스트·도구용).
- 결과는 `ParseResult`: `status`가 `ok`(섹션 있음), `no_text`(추출 텍스트 0자), `failed`(사유 코드).
- 지원 형식: PDF, DOCX, PPTX, TXT, MD. HWP·HWPX·XLSX는 `unsupported_format`(T18에서 지원).
"""

from app.ingest.parsers.base import (
    FAILURE_GUIDANCE,
    WARNING_SCANNED_PAGE,
    ParseFailure,
    ParseOutput,
    Parser,
    ParseResult,
    ParseWarning,
    Section,
)
from app.ingest.parsers.registry import (
    DEFERRED_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    get_parser,
    is_supported,
    parse_document,
)

__all__ = [
    "DEFERRED_EXTENSIONS",
    "FAILURE_GUIDANCE",
    "SUPPORTED_EXTENSIONS",
    "WARNING_SCANNED_PAGE",
    "ParseFailure",
    "ParseOutput",
    "ParseResult",
    "ParseWarning",
    "Parser",
    "Section",
    "get_parser",
    "is_supported",
    "parse_document",
]
