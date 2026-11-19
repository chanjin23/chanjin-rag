"""구조 기반 청킹: 500~800토큰, 10~15% 겹침, 표 헤더 반복, 슬라이드 단위 (TRD 6.3)."""

from app.ingest.chunking.chunker import (
    MAX_TOKENS,
    MIN_TOKENS,
    TABLE_MAX_TOKENS,
    Chunk,
    build_chunks,
    embed_input,
    filename_chunk,
    heading_prefix,
    merge_locations,
    overlap_text,
)
from app.ingest.chunking.tokens import count_tokens

__all__ = [
    "MAX_TOKENS",
    "MIN_TOKENS",
    "TABLE_MAX_TOKENS",
    "Chunk",
    "build_chunks",
    "count_tokens",
    "embed_input",
    "filename_chunk",
    "heading_prefix",
    "merge_locations",
    "overlap_text",
]
