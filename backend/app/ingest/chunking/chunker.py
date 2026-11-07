"""구조 기반 청킹 (TRD 6.3, PRD 5.1-5).

파서가 만든 `Section` 목록(문단·표 행·슬라이드·페이지 단위)을 검색 단위 `Chunk`로 묶는다.

- 본문(`kind=text`): 연속하고 `heading_path`가 같은 섹션을 이어 붙여 500~800토큰으로 만든다.
  문단(섹션 안에서는 빈 줄로 나눈 덩어리) 중간에서는 자르지 않는다.
  800토큰을 넘는 문단만 문장 단위로, 문장도 넘으면 단어 단위, 그래도 넘으면 토큰 단위로 자른다.
  같은 묶음의 두 번째 청크부터는 앞 청크 끝 10~15%(토큰 기준, 문장 시작 우선,
  없으면 단어 경계)를 앞에 겹쳐 붙인다.
- 표(`kind=table`): 같은 표(위치에서 `row`를 뺀 값이 같음)의 행을 최대 약 600토큰씩 묶는다.
  행 텍스트가 `헤더: 값 | 헤더: 값` 형식이라 청크마다 헤더가 반복된다.
  형식(DOCX·PPTX·XLSX)에 기대지 않는다.
- 슬라이드(`kind=slide`): 슬라이드 1장 = 청크 1개(발표자 노트 포함).
  800토큰을 넘으면 줄 단위로 나눈다.
- 임베딩 입력(`embed_text`)에만 `[파일명 > 제목 > 소제목]` 머리말을 붙인다.
  화면용 `content`에는 없다. `content_sha256`은 임베딩 입력의 해시다(같으면 기존 임베딩 재사용).
- `meta_only`(10MB 초과 등)·`no_text`·미지원 형식: 파일명·경로만 담은 청크 1개(`embeddable=False`,
  BM25로만 검색).
- `location`: 섹션 위치를 그대로 쓴다. 여러 섹션을 합친 청크는 처음 값을 두고 달라지는 키에
  `<키>_end`를 더한다(예: `{"page": 3, "page_end": 4}`, `{"table": 2, "row": 2, "row_end": 9}`).
  `lines`는 `[처음 줄, 마지막 줄]`로 합친다.
- `bm25_text`는 화면용 `content`를 `app.ingest.tokenize.bm25_text`(질문과 같은 함수)로
  토큰화한 값이다. 파일명·제목은 BM25 색인의 `filename`·`heading_path` 컬럼이 따로 맡는다.
"""

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.ingest.chunking.tokens import count_tokens, head_by_tokens, tail_by_tokens
from app.ingest.parsers.base import Section
from app.ingest.tokenize import bm25_text

MIN_TOKENS = 500
MAX_TOKENS = 800
TABLE_MAX_TOKENS = 600
OVERLAP_MIN_RATIO = 0.10
OVERLAP_MAX_RATIO = 0.15

ChunkKind = Literal["text", "table", "slide", "meta"]

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n\s*")
_SENTENCE_BREAK = re.compile(r"((?<=[.!?。？！…])\s+|\s*\n\s*)")
_WORD_BREAK = re.compile(r"(\s+)")
_WORD_START = re.compile(r"(?<=\s)\S")
_SENTENCE_END = re.compile(r"[.!?。？！…]$")
_ROW_KEYS = frozenset({"row", "rows"})


@dataclass(frozen=True)
class Chunk:
    seq: int
    kind: ChunkKind
    location: dict[str, Any]
    heading_path: list[str]
    content: str  # 화면용(머리말 없음)
    embed_text: str  # 임베딩 입력(머리말 + content)
    content_sha256: str  # embed_text의 SHA-256
    bm25_text: str
    token_count: int  # content 토큰 수(o200k_base)
    embeddable: bool = True


@dataclass(frozen=True)
class _Unit:
    text: str
    location: dict[str, Any]
    sep: str = "\n\n"  # 앞 단위와 이을 때 넣는 구분자


@dataclass
class _Draft:
    kind: ChunkKind
    heading_path: list[str]
    content: str
    locations: list[dict[str, Any]] = field(default_factory=list)


def heading_prefix(filename: str, heading_path: Sequence[str]) -> str:
    return "[" + " > ".join([filename, *heading_path]) + "]"


def embed_input(filename: str, heading_path: Sequence[str], content: str) -> str:
    return f"{heading_prefix(filename, heading_path)}\n{content}"


def build_chunks(
    sections: Sequence[Section],
    *,
    filename: str,
    path: str = "",
    meta_only: bool = False,
) -> list[Chunk]:
    """문서 하나의 청크 목록. 텍스트가 없거나 `meta_only`면 파일명 청크 1개."""
    usable = [s for s in sections if s.text.strip()]
    if meta_only or not usable:
        return [filename_chunk(filename, path)]

    drafts: list[_Draft] = []
    for kind, group in _groups(usable):
        if kind == "table":
            drafts.extend(_table_drafts(group))
        elif kind == "slide":
            drafts.extend(_slide_drafts(group[0]))
        else:
            drafts.extend(_text_drafts(group))
    return [_finalize(i, d, filename) for i, d in enumerate(drafts)]


def filename_chunk(filename: str, path: str = "") -> Chunk:
    """`meta_only`·`no_text`·미지원 형식 문서의 파일명 청크(BM25 전용, TRD 6.3)."""
    content = filename if not path or path == filename else f"{filename}\n{path}"
    embed_text = embed_input(filename, [], content)
    return Chunk(
        seq=0,
        kind="meta",
        location={},
        heading_path=[],
        content=content,
        embed_text=embed_text,
        content_sha256=_sha256(embed_text),
        bm25_text=bm25_text(content),
        token_count=count_tokens(content),
        embeddable=False,
    )


# ---------------------------------------------------------------- 묶기


def _group_key(section: Section, index: int) -> tuple[Any, ...]:
    if section.kind == "slide":
        return ("slide", index)  # 슬라이드는 언제나 따로
    heading = tuple(section.heading_path)
    if section.kind == "table":
        table_id = json.dumps(
            {k: v for k, v in section.location.items() if k not in _ROW_KEYS},
            sort_keys=True,
            ensure_ascii=False,
        )
        return ("table", heading, table_id)
    return ("text", heading)


def _groups(sections: Sequence[Section]) -> list[tuple[str, list[Section]]]:
    out: list[tuple[str, list[Section]]] = []
    last: tuple[Any, ...] | None = None
    for i, s in enumerate(sections):
        key = _group_key(s, i)
        if out and key == last:
            out[-1][1].append(s)
        else:
            out.append((str(key[0]), [s]))
        last = key
    return out


def _text_drafts(group: list[Section]) -> list[_Draft]:
    units: list[_Unit] = []
    for s in group:
        for para in _PARAGRAPH_BREAK.split(s.text.strip()):
            para = para.strip()
            if not para:
                continue
            if count_tokens(para) <= MAX_TOKENS:
                units.append(_Unit(para, s.location, "\n\n"))
            else:
                pieces = _split_long(para, MAX_TOKENS)
                units.extend(
                    _Unit(text, s.location, "\n\n" if i == 0 else sep)
                    for i, (text, sep) in enumerate(pieces)
                )
    return _pack(units, "text", list(group[0].heading_path), MAX_TOKENS, overlap=True)


def _slide_drafts(section: Section) -> list[_Draft]:
    text = section.text.strip()
    heading = list(section.heading_path)
    if count_tokens(text) <= MAX_TOKENS:
        return [_Draft("slide", heading, text, [section.location])]
    units: list[_Unit] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if count_tokens(line) <= MAX_TOKENS:
            units.append(_Unit(line, section.location, "\n"))
        else:
            pieces = _split_long(line, MAX_TOKENS)
            units.extend(
                _Unit(t, section.location, "\n" if i == 0 else sep)
                for i, (t, sep) in enumerate(pieces)
            )
    return _pack(units, "slide", heading, MAX_TOKENS, overlap=True)


def _table_drafts(group: list[Section]) -> list[_Draft]:
    units: list[_Unit] = []
    for s in group:
        row = s.text.strip()
        if count_tokens(row) <= MAX_TOKENS:
            units.append(_Unit(row, s.location, "\n"))
        else:  # 아주 긴 행 한 줄: 셀 경계(` | `)를 단어 경계로 보고 나눈다
            pieces = _split_long(row, MAX_TOKENS)
            units.extend(
                _Unit(t, s.location, "\n" if i == 0 else sep) for i, (t, sep) in enumerate(pieces)
            )
    return _pack(units, "table", list(group[0].heading_path), TABLE_MAX_TOKENS, overlap=False)


def _pack(
    units: list[_Unit],
    kind: ChunkKind,
    heading_path: list[str],
    max_tokens: int,
    *,
    overlap: bool,
) -> list[_Draft]:
    """단위를 순서대로 `max_tokens`까지 채운다.

    단위 하나가 한도를 넘으면 그 단위만으로 청크를 만든다.
    """
    drafts: list[_Draft] = []
    prefix = ""
    current: list[_Unit] = []

    def emit() -> None:
        nonlocal prefix, current
        content = _join(prefix, current)
        drafts.append(_Draft(kind, heading_path, content, [u.location for u in current]))
        prefix = overlap_text(content) if overlap else ""
        current = []

    for unit in units:
        if current:
            if count_tokens(_join(prefix, [*current, unit])) <= max_tokens:
                current.append(unit)
                continue
            emit()
        # 새 청크 시작. 겹침을 붙이면 한도를 넘는 경우에는 겹침을 뺀다(크기 상한이 우선)
        if prefix and count_tokens(_join(prefix, [unit])) > max_tokens:
            prefix = ""
        current = [unit]
    if current:
        emit()
    return drafts


def _join(prefix: str, units: Sequence[_Unit]) -> str:
    text = prefix
    for u in units:
        text = f"{text}{u.sep}{u.text}" if text else u.text
    return text


# ---------------------------------------------------------------- 자르기·겹침


def _split_long(text: str, max_tokens: int) -> list[tuple[str, str]]:
    """너무 긴 덩어리를 (조각, 앞 조각과의 구분자) 목록으로. 문장 → 단어 → 토큰 순으로 쪼갠다."""
    out: list[tuple[str, str]] = []
    for sentence, sep in _pieces(text, _SENTENCE_BREAK):
        if count_tokens(sentence) <= max_tokens:
            out.append((sentence, sep))
            continue
        first = True
        for chunk in _pack_words(sentence, max_tokens):
            out.append((chunk, sep if first else " "))
            first = False
    return out


def _pieces(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """`pattern`(그룹 하나로 구분자를 잡음)으로 나눈 조각과 각 조각 앞의 구분자."""
    parts = pattern.split(text)
    out: list[tuple[str, str]] = []
    sep = ""
    for i, part in enumerate(parts):
        if i % 2 == 1:
            sep = "\n" if "\n" in part else " "
            continue
        part = part.strip()
        if part:
            out.append((part, sep))
    return out


def _pack_words(sentence: str, max_tokens: int) -> list[str]:
    words = [w for w, _ in _pieces(sentence, _WORD_BREAK)]
    out: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if count_tokens(candidate) <= max_tokens:
            current = candidate
            continue
        if current:
            out.append(current)
        current = word
        while count_tokens(current) > max_tokens:  # 공백 없는 아주 긴 덩어리
            head, current = head_by_tokens(current, max_tokens)
            out.append(head)
    if current:
        out.append(current)
    return out


def overlap_text(content: str) -> str:
    """다음 청크 앞에 붙일 `content` 끝 부분(토큰 기준 10~15%).

    문장 시작을 우선하고, 없으면 단어 경계, 그것도 없으면 토큰 경계에서 자른다.
    """
    total = count_tokens(content)
    lo = math.ceil(total * OVERLAP_MIN_RATIO)
    hi = math.floor(total * OVERLAP_MAX_RATIO)
    if hi < 1:
        return ""
    word_start: int | None = None
    for m in reversed(list(_WORD_START.finditer(content))):
        i = m.start()
        n = count_tokens(content[i:])
        if n > hi:
            break
        if n < lo:
            continue
        before = content[:i]
        if "\n" in before[len(before.rstrip()) :] or _SENTENCE_END.search(before.rstrip()):
            return content[i:]
        if word_start is None:
            word_start = i
    if word_start is not None:
        return content[word_start:]
    return tail_by_tokens(content, round(total * (OVERLAP_MIN_RATIO + OVERLAP_MAX_RATIO) / 2))


# ---------------------------------------------------------------- 마무리


def merge_locations(locations: Sequence[dict[str, Any]]) -> dict[str, Any]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for loc in locations:
        key = json.dumps(loc, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            unique.append(loc)
    if not unique:
        return {}
    if len(unique) == 1:
        return dict(unique[0])
    keys: list[str] = []
    for loc in unique:
        keys.extend(k for k in loc if k not in keys)
    merged: dict[str, Any] = {}
    for k in keys:
        values = [loc[k] for loc in unique if k in loc]
        if k == "lines" and all(_is_line_range(v) for v in values):
            merged[k] = [min(v[0] for v in values), max(v[1] for v in values)]
        elif all(v == values[0] for v in values):
            merged[k] = values[0]
        else:
            merged[k] = values[0]
            merged[f"{k}_end"] = values[-1]
    return merged


def _is_line_range(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) for x in v)


def _finalize(seq: int, draft: _Draft, filename: str) -> Chunk:
    embed_text = embed_input(filename, draft.heading_path, draft.content)
    return Chunk(
        seq=seq,
        kind=draft.kind,
        location=merge_locations(draft.locations),
        heading_path=list(draft.heading_path),
        content=draft.content,
        embed_text=embed_text,
        content_sha256=_sha256(embed_text),
        bm25_text=bm25_text(draft.content),
        token_count=count_tokens(draft.content),
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
