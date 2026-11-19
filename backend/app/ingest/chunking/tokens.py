"""청크 길이 기준 토큰 수 (tiktoken o200k_base 근사, TRD 2장).

실제 임베딩 모델의 토크나이저와 다를 수 있어 근사치다. 한도 초과는 API의 `truncate=END`가 막는다.
인코딩 파일은 처음 쓸 때 내려받아 캐시한다(`TIKTOKEN_CACHE_DIR`로 위치를 정할 수 있음).
"""

from functools import lru_cache
from typing import Any

import tiktoken

ENCODING_NAME = "o200k_base"


@lru_cache(maxsize=1)
def _encoding() -> Any:
    return tiktoken.get_encoding(ENCODING_NAME)


def encode(text: str) -> list[int]:
    # 문서 안의 `<|endoftext|>` 같은 문자열도 일반 텍스트로 센다(특수 토큰 오류를 내지 않음)
    tokens: list[int] = _encoding().encode(text, disallowed_special=())
    return tokens


def count_tokens(text: str) -> int:
    return len(encode(text)) if text else 0


def head_by_tokens(text: str, max_tokens: int) -> tuple[str, str]:
    """앞에서 `max_tokens` 이하가 되는 가장 긴 부분과 나머지로 나눈다(글자 경계 유지)."""
    tokens = encode(text)
    if len(tokens) <= max_tokens:
        return text, ""
    _, offsets = _encoding().decode_with_offsets(tokens)
    cut = max(1, int(offsets[max_tokens]))
    while cut > 1 and count_tokens(text[:cut]) > max_tokens:
        cut -= 1
    return text[:cut], text[cut:]


def tail_by_tokens(text: str, n_tokens: int) -> str:
    """끝에서 약 `n_tokens`개 토큰에 해당하는 부분(글자 경계 유지)."""
    tokens = encode(text)
    if len(tokens) <= n_tokens:
        return text
    _, offsets = _encoding().decode_with_offsets(tokens)
    start = int(offsets[len(tokens) - n_tokens])
    return text[start:]
