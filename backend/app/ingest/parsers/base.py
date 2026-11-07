"""파서 공통 형식 (TRD 3.4).

- 파서는 `Parser`를 구현하고 `Section` 목록을 돌려준다. 실패는 `ParseFailure(code)`로 알린다.
- 실행 결과는 `ParseResult` 하나로 모은다: 성공(`ok`), 텍스트 없음(`no_text`),
  실패(`failed` + 사유 코드).
- 경고(예: PDF 스캔본 의심 페이지)는 실패가 아니므로 `warnings`에 담는다.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from app.db.enums import FailureCode

SectionKindLiteral = Literal["text", "table", "slide"]
ParseStatus = Literal["ok", "no_text", "failed"]

# 화면 안내 문구 (TRD 3.4 실패 사유 표)
FAILURE_GUIDANCE: dict[FailureCode, str] = {
    FailureCode.ENCRYPTED: "암호를 풀고 저장하면 다음 동기화 때 다시 처리됩니다",
    FailureCode.CORRUPTED: "파일을 다시 저장해 보세요",
    FailureCode.TIMEOUT: "재처리하거나 파일을 나눠 보세요",
    FailureCode.MEMORY: "재처리하거나 파일을 나눠 보세요",
    FailureCode.HASH_MISMATCH: "다음 동기화 때 자동으로 다시 올라옵니다",
    FailureCode.PARSER_ERROR: "재처리하세요. 반복되면 파서 문제입니다",
    FailureCode.UNSUPPORTED_FORMAT: "이 형식은 아직 지원하지 않습니다. 후속 버전에서 지원합니다",
}

# 자식 프로세스가 결과 JSON 줄 앞에 붙이는 표시(isolation, _child)
RESULT_PREFIX = b"@@PARSE_RESULT@@"

# 경고 코드
WARNING_SCANNED_PAGE = "scanned_page"  # PDF 페이지에 텍스트 레이어가 없음(스캔본 의심, 건너뜀)


@dataclass(frozen=True)
class Section:
    location: dict[str, Any]  # 예: {"page": 3}, {"slide": 7}, {"lines": [10, 14]}
    text: str
    kind: SectionKindLiteral = "text"
    heading_path: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "heading_path": list(self.heading_path),
            "text": self.text,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Section":
        return cls(
            location=dict(d["location"]),
            text=str(d["text"]),
            kind=d.get("kind", "text"),
            heading_path=[str(h) for h in d.get("heading_path", [])],
        )


@dataclass(frozen=True)
class ParseWarning:
    code: str
    location: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "location": self.location}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ParseWarning":
        return cls(code=str(d["code"]), location=dict(d.get("location", {})))


@dataclass
class ParseOutput:
    """파서 하나가 돌려주는 값. 섹션과 경고, 부가 정보(예: 판별한 인코딩)."""

    sections: list[Section]
    warnings: list[ParseWarning] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class ParseFailure(Exception):
    """사유 코드가 정해진 파싱 실패. `detail`은 DB의 `failure_detail`로 간다."""

    def __init__(self, code: FailureCode, detail: str | None = None) -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail


class Parser(Protocol):
    extensions: tuple[str, ...]

    def parse(self, path: Path) -> ParseOutput: ...


@dataclass
class ParseResult:
    status: ParseStatus
    sections: list[Section] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    failure_code: FailureCode | None = None
    failure_detail: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text_chars(self) -> int:
        return sum(len(s.text) for s in self.sections)

    @property
    def guidance(self) -> str | None:
        """실패 시 화면 안내 문구."""
        return FAILURE_GUIDANCE.get(self.failure_code) if self.failure_code else None

    @classmethod
    def failed(cls, code: FailureCode, detail: str | None = None) -> "ParseResult":
        return cls(status="failed", failure_code=code, failure_detail=detail)

    @classmethod
    def from_output(cls, out: ParseOutput) -> "ParseResult":
        sections = [s for s in out.sections if s.text.strip()]
        status: ParseStatus = "ok" if sections else "no_text"
        return cls(status=status, sections=sections, warnings=out.warnings, meta=out.meta)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "sections": [s.to_dict() for s in self.sections],
            "warnings": [w.to_dict() for w in self.warnings],
            "failure_code": self.failure_code.value if self.failure_code else None,
            "failure_detail": self.failure_detail,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ParseResult":
        code = d.get("failure_code")
        return cls(
            status=d["status"],
            sections=[Section.from_dict(s) for s in d.get("sections", [])],
            warnings=[ParseWarning.from_dict(w) for w in d.get("warnings", [])],
            failure_code=FailureCode(code) if code else None,
            failure_detail=d.get("failure_detail"),
            meta=dict(d.get("meta", {})),
        )
