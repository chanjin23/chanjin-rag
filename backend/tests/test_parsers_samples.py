"""실제 샘플 문서(SAMPLE_DOCS_DIRS)로 파서를 돌려 형식별 성공률을 본다.

`pytest -m samples`로만 실행한다.

개인·고객사 문서이므로 내용·파일명·경로를 출력하거나 기록하지 않는다. 형식별 개수와 비율만 다룬다.
- 대상: 지원 형식(MD·PDF·TXT·DOCX·PPTX). Office 임시 파일(`~$`)과 10MB 초과 파일(업로드하지 않고
  파일명만 색인, PRD 5.1-3)은 뺀다.
- 실제 Worker 경로와 같게 하위 프로세스(`parse_in_subprocess`)에서 파싱한다.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.db.enums import FailureCode
from app.ingest.parsers import SUPPORTED_EXTENSIONS, ParseResult
from app.ingest.parsers.isolation import parse_in_subprocess

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
PARSER_FAILURE_CODES = {
    FailureCode.ENCRYPTED,
    FailureCode.CORRUPTED,
    FailureCode.TIMEOUT,
    FailureCode.MEMORY,
    FailureCode.PARSER_ERROR,
}


@dataclass
class FormatStats:
    total: int = 0
    ok: int = 0
    no_text: int = 0
    with_sections: int = 0
    failures: Counter[str] = field(default_factory=Counter)
    failures_without_code: int = 0

    @property
    def success_rate(self) -> float:
        # no_text는 실패가 아니다(파일명 청크로 검색됨, TRD 3.4)
        return (self.ok + self.no_text) / self.total if self.total else 0.0


def _iter_sample_files(dirs: list[Path]) -> Iterator[Path]:
    for d in dirs:
        for p in d.rglob("*"):
            if p.suffix.lower() not in SUPPORTED_EXTENSIONS or p.name.startswith("~$"):
                continue
            if not p.is_file() or p.stat().st_size > MAX_UPLOAD_BYTES:
                continue
            yield p


_cache: dict[tuple[Path, ...], dict[str, FormatStats]] = {}


def collect_stats(dirs: list[Path]) -> dict[str, FormatStats]:
    key = tuple(dirs)
    if key in _cache:
        return _cache[key]
    stats: dict[str, FormatStats] = {}
    for path in _iter_sample_files(dirs):
        ext = path.suffix.lower()
        s = stats.setdefault(ext, FormatStats())
        result: ParseResult = parse_in_subprocess(path)
        s.total += 1
        if result.sections:
            s.with_sections += 1
        if result.status == "ok":
            s.ok += 1
        elif result.status == "no_text":
            s.no_text += 1
        elif result.failure_code is None:
            s.failures_without_code += 1
        else:
            s.failures[result.failure_code.value] += 1
    _cache[key] = stats
    return stats


def _report(stats: dict[str, FormatStats]) -> str:
    lines = ["형식별 파싱 결과(개수만):"]
    for ext in sorted(stats):
        s = stats[ext]
        failures = ", ".join(f"{code} {n}" for code, n in sorted(s.failures.items())) or "없음"
        lines.append(
            f"  {ext}: 전체 {s.total}, 성공 {s.ok}, no_text {s.no_text}, "
            f"성공률 {s.success_rate:.1%}, 실패 사유: {failures}"
        )
    return "\n".join(lines)


@pytest.mark.samples
def test_sample_docs_parse(sample_docs_dirs: list[Path]) -> None:
    stats = collect_stats(sample_docs_dirs)
    print("\n" + _report(stats))

    assert sum(s.total for s in stats.values()) > 0, "지원 형식의 샘플 문서가 없습니다"
    for ext, s in stats.items():
        # 실패한 문서에는 반드시 사유 코드가 붙고, 파서 실패 코드 중 하나여야 한다
        assert s.failures_without_code == 0, (
            f"{ext}: 사유 코드 없는 실패 {s.failures_without_code}개"
        )
        unknown = set(s.failures) - {c.value for c in PARSER_FAILURE_CODES}
        assert not unknown, f"{ext}: 예상하지 못한 사유 코드 {sorted(unknown)}"
        # 텍스트 형식은 인코딩 판별만 되면 모두 읽혀야 한다
        if ext in {".md", ".txt"}:
            assert s.success_rate == 1.0, _report(stats)


@pytest.mark.samples
def test_sample_docx_and_pptx_parse(sample_docs_dirs: list[Path]) -> None:
    stats = collect_stats(sample_docs_dirs)
    for ext in (".docx", ".pptx"):
        s = stats.get(ext)
        assert s is not None and s.total >= 1, f"{ext} 샘플이 없습니다"
        assert s.with_sections >= 1, f"{ext}: 섹션이 나온 문서가 없습니다\n{_report(stats)}"
        # 성공한 문서는 모두 섹션이 1개 이상(ok의 정의)
        assert s.with_sections == s.ok, _report(stats)
