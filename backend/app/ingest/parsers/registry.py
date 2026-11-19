"""확장자 → 파서 등록표와 프로세스 안 파싱 (TRD 3.4).

- 확장자는 대소문자를 무시한다.
- 파서 모듈은 처음 쓸 때 import한다(쓰지 않는 파서 라이브러리를 읽지 않게).
- 아직 파서가 없는 형식(HWP·HWPX·XLSX, T18)과 등록되지 않은 형식은 파싱하지 않고
  `unsupported_format`.
- 이 모듈의 `parse_document`는 같은 프로세스에서 돈다.
  Worker는 `isolation.parse_in_subprocess`를 쓴다.
"""

import importlib
from pathlib import Path

from app.db.enums import FailureCode
from app.ingest.parsers.base import ParseFailure, Parser, ParseResult

# 확장자 → "모듈:클래스"
_REGISTRY: dict[str, str] = {
    ".pdf": "app.ingest.parsers.pdf_parser:PdfParser",
    ".docx": "app.ingest.parsers.docx_parser:DocxParser",
    ".pptx": "app.ingest.parsers.pptx_parser:PptxParser",
    ".txt": "app.ingest.parsers.text_parser:TextParser",
    ".md": "app.ingest.parsers.text_parser:TextParser",
}

# 업로드는 받지만 아직 파서가 없는 형식. T18에서 파서를 만들고 여기서 뺀다.
DEFERRED_EXTENSIONS: frozenset[str] = frozenset({".hwp", ".hwpx", ".xlsx"})

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(_REGISTRY)

_DETAIL_MAX = 500


def normalize_extension(path_or_ext: str | Path) -> str:
    s = str(path_or_ext)
    if s.startswith(".") and "/" not in s and "\\" not in s:
        return s.lower()
    return Path(s).suffix.lower()


def is_supported(path_or_ext: str | Path) -> bool:
    return normalize_extension(path_or_ext) in _REGISTRY


def load_ref(ref: str) -> Parser:
    """`모듈:이름`을 읽어 파서 객체를 만든다(클래스면 인스턴스로)."""
    module_name, _, attr = ref.partition(":")
    obj = getattr(importlib.import_module(module_name), attr)
    return obj() if isinstance(obj, type) else obj  # type: ignore[no-any-return]


def get_parser(path_or_ext: str | Path) -> Parser | None:
    ref = _REGISTRY.get(normalize_extension(path_or_ext))
    return load_ref(ref) if ref else None


def unsupported_result(path_or_ext: str | Path) -> ParseResult:
    ext = normalize_extension(path_or_ext) or "(확장자 없음)"
    return ParseResult.failed(FailureCode.UNSUPPORTED_FORMAT, f"{ext} 형식은 아직 파서가 없음")


def _scrub(message: str, path: Path) -> str:
    """예외 메시지에서 파일 경로를 지운다(로그·화면에 경로가 두 번 남지 않게)."""
    for p in {str(path), str(path.resolve()), path.as_posix()}:
        message = message.replace(p, "<file>")
    return message[:_DETAIL_MAX]


def run_parser(parser: Parser, path: Path) -> ParseResult:
    """파서를 실행하고 예외를 사유 코드로 바꾼다. 텍스트가 0자면 `no_text`."""
    try:
        out = parser.parse(path)
    except ParseFailure as e:
        return ParseResult.failed(e.code, e.detail)
    except MemoryError:
        return ParseResult.failed(FailureCode.MEMORY, "메모리 상한 초과")
    except Exception as e:  # 파서 라이브러리의 그 밖의 예외
        return ParseResult.failed(
            FailureCode.PARSER_ERROR, _scrub(f"{type(e).__name__}: {e}", path)
        )
    return ParseResult.from_output(out)


def parse_document(path: Path) -> ParseResult:
    """같은 프로세스에서 파싱한다(테스트·도구용). 지원하지 않는 형식은 파싱하지 않는다."""
    parser = get_parser(path)
    if parser is None:
        return unsupported_result(path)
    return run_parser(parser, path)
