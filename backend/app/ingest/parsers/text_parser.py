"""TXT·MD 파서 (charset-normalizer, TRD 3.4).

- 인코딩: BOM이 있거나 UTF-8로 읽히면 UTF-8, 아니면 charset-normalizer로 판별한다.
  한국어 Windows 파일(cp949)이 많아 판별 결과가 EUC-KR 계열이면 상위 집합인 cp949로 읽는다.
- 빈 줄로 나눈 문단 1개 = 섹션 1개, 위치 `{"lines": [시작, 끝]}`(1부터, 끝 포함).
- MD: ATX 제목(`#`~`######`)으로 `heading_path`를 만든다.
  코드 블록(``` / ~~~) 안의 `#`은 제목이 아니다.
  제목 줄 자체는 섹션으로 만들지 않고 경로로만 쓴다.
"""

import re
from pathlib import Path

from charset_normalizer import from_bytes

from app.db.enums import FailureCode
from app.ingest.parsers.base import ParseFailure, ParseOutput, Section

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_KOREAN_LEGACY = {"cp949", "euc_kr", "euc-kr", "ms949", "uhc", "johab", "iso2022_kr"}


def decode_text(data: bytes) -> tuple[str, str]:
    """(본문, 인코딩 이름). 판별하지 못하면 `corrupted`."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    best = from_bytes(data).best()
    if best is None:
        # 판별 실패. 한국어 Windows 파일일 가능성이 가장 크므로 cp949를 마지막으로 시도한다.
        try:
            return data.decode("cp949"), "cp949"
        except UnicodeDecodeError as e:
            raise ParseFailure(FailureCode.CORRUPTED, "텍스트 인코딩을 판별하지 못함") from e
    encoding = best.encoding.lower()
    if encoding in _KOREAN_LEGACY:
        try:
            return data.decode("cp949"), "cp949"
        except UnicodeDecodeError:
            pass
    return str(best), encoding


def split_blocks(lines: list[str], *, markdown: bool) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    block: list[str] = []
    start = 0
    in_fence: str | None = None

    def flush(end: int) -> None:
        nonlocal block
        text = "\n".join(block).strip()
        if text:
            path = [t for _, t in stack]
            sections.append(Section({"lines": [start, end]}, text, "text", path))
        block = []

    for no, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r")
        if markdown:
            fence = _FENCE.match(line)
            if fence:
                marker = fence.group(1)[0]
                if in_fence is None:
                    in_fence = marker
                elif in_fence == marker:
                    in_fence = None
            elif in_fence is None:
                m = _ATX.match(line)
                if m:
                    flush(no - 1)
                    level = len(m.group(1))
                    title = (m.group(2) or "").strip()
                    while stack and stack[-1][0] >= level:
                        stack.pop()
                    if title:
                        stack.append((level, title))
                    continue
        if not line.strip() and in_fence is None:
            flush(no - 1)
            continue
        if not block:
            start = no
        block.append(line)
    flush(len(lines))
    return sections


class TextParser:
    extensions: tuple[str, ...] = (".txt", ".md")

    def parse(self, path: Path) -> ParseOutput:
        data = path.read_bytes()
        if b"\x00" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise ParseFailure(FailureCode.CORRUPTED, "텍스트 파일이 아님(NUL 바이트)")
        text, encoding = decode_text(data)
        markdown = path.suffix.lower() == ".md"
        sections = split_blocks(text.split("\n"), markdown=markdown)
        return ParseOutput(sections=sections, meta={"encoding": encoding})
