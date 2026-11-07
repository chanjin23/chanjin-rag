"""문서 파서 공통 형식 (TRD 3.4).

형식별 구현(pdf, docx, xlsx, pptx, hwp, hwpx, text)은 이후 추가한다.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class Section:
    location: dict[str, Any]  # 예: {"page": 3}, {"sheet": "매출", "rows": [2, 41]}, {"slide": 7}
    text: str
    kind: Literal["text", "table", "slide"] = "text"
    heading_path: list[str] = field(default_factory=list)


class Parser(Protocol):
    extensions: tuple[str, ...]

    def parse(self, path: Path) -> list[Section]: ...
