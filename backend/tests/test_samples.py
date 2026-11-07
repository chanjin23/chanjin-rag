"""실제 샘플 문서 폴더(SAMPLE_DOCS_DIRS) 확인. 기본 실행에서는 빠지고 `pytest -m samples`로만 돈다.

개인·고객사 문서이므로 내용이나 파일명을 출력·기록하지 않고 개수만 다룬다.
"""

from pathlib import Path

import pytest

DOC_EXTS = {".md", ".txt", ".pdf", ".docx", ".pptx"}


@pytest.mark.samples
def test_sample_docs_dirs_have_documents(sample_docs_dirs: list[Path]) -> None:
    counts = [
        sum(1 for p in d.rglob("*") if p.suffix.lower() in DOC_EXTS) for d in sample_docs_dirs
    ]
    assert all(c > 0 for c in counts), f"문서가 없는 폴더가 있습니다(폴더별 개수: {counts})"
