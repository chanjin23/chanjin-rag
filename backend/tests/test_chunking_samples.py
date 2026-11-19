"""실제 샘플 문서(SAMPLE_DOCS_DIRS)를 파싱해 청킹 규칙이 지켜지는지 본다.

`pytest -m samples`로만 실행한다.

개인·고객사 문서이므로 내용·파일명·경로를 출력하지 않는다. 개수와 토큰 분포만 다룬다.
"""

from collections import Counter
from pathlib import Path

import pytest

from app.ingest.chunking import MAX_TOKENS, MIN_TOKENS, build_chunks, count_tokens
from app.ingest.parsers import SUPPORTED_EXTENSIONS, parse_document

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@pytest.mark.samples
def test_sample_docs_chunking(sample_docs_dirs: list[Path]) -> None:
    files = 0
    kinds: Counter[str] = Counter()
    below_min = 0
    for root in sample_docs_dirs:
        for path in root.rglob("*"):
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS or path.name.startswith("~$"):
                continue
            if not path.is_file() or path.stat().st_size > MAX_UPLOAD_BYTES:
                continue
            result = parse_document(path)
            chunks = build_chunks(
                result.sections,
                filename=path.name,
                path=path.relative_to(root).as_posix(),
                meta_only=result.status == "failed",
            )
            files += 1
            assert chunks, "문서마다 청크가 1개 이상이어야 한다"
            assert [c.seq for c in chunks] == list(range(len(chunks)))
            for c in chunks:
                kinds[c.kind] += 1
                # 실패 메시지에 본문·파일명이 찍히지 않도록 참·거짓만 검사한다
                has_text = bool(c.content.strip())
                within_max = c.token_count == count_tokens(c.content) <= MAX_TOKENS
                has_prefix = c.embed_text.startswith(f"[{path.name}")
                assert has_text, "빈 청크"
                assert within_max, f"{c.kind} 청크가 {MAX_TOKENS}토큰을 넘음: {c.token_count}"
                assert has_prefix, "임베딩 입력에 머리말이 없음"
                below_min += c.token_count < MIN_TOKENS
    total = sum(kinds.values())
    print(
        f"\n샘플 청킹: 문서 {files}, 청크 {total}, 종류 {dict(kinds)}, "
        f"{MIN_TOKENS}토큰 미만 {below_min} ({below_min / max(total, 1):.0%})"
    )
    assert files > 0
