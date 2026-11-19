"""답변 후처리 (TRD 7.1 "답변 후처리").

- 추론 텍스트 제거: 답 앞에 `<think>…</think>`가 섞여 오면 버린다(Provider도 거르지만 그래프에서도
  한 번 더 거른다. 다른 Provider로 바꿔도 저장·화면에 추론이 남지 않게).
- 없는 인용 번호 제거: 자료가 k개인데 `[k+1]`이나 `[0]`을 달면 지운다. `[1, 9]`처럼 섞여 있으면
  맞는 번호만 남긴다. 지운 인용 앞의 공백도 함께 지운다("내용 [9]." → "내용.").
- 토큰 스트리밍 중에 처리한다. 인용이 조각 경계에서 나뉘어 와도(`[1`, `2]`) 맞게 동작하도록
  닫히지 않은 `[숫자`와 끝의 공백은 다음 조각이 올 때까지 잡아 둔다.
"""

import re
from collections.abc import Collection

from app.providers.nvidia import ThinkingFilter

CITATION_RE = re.compile(r"(\s*)\[(\d+(?:\s*,\s*\d+)*)\]")
# 아직 닫히지 않았지만 인용이 될 수 있는 꼬리
_PARTIAL_RE = re.compile(r"\[[\d,\s]*")
_MAX_PARTIAL = 24


def cited_numbers(text: str) -> list[int]:
    """답변에 나온 인용 번호(처음 나온 순서, 중복 없이)."""
    out: list[int] = []
    for m in CITATION_RE.finditer(text):
        for part in m.group(2).split(","):
            n = int(part.strip())
            if n not in out:
                out.append(n)
    return out


class CitationFilter:
    """`valid`에 없는 인용 번호를 지운다(스트리밍)."""

    def __init__(self, valid: Collection[int]) -> None:
        self.valid = set(valid)
        self.removed = 0
        self._buf = ""

    def _clean(self, text: str) -> str:
        def repl(m: re.Match[str]) -> str:
            numbers = [int(p.strip()) for p in m.group(2).split(",")]
            kept = [n for n in numbers if n in self.valid]
            self.removed += len(numbers) - len(kept)
            if not kept:
                return ""
            if len(kept) == len(numbers):
                return m.group(0)
            return f"{m.group(1)}[{', '.join(str(n) for n in kept)}]"

        return CITATION_RE.sub(repl, text)

    def feed(self, text: str) -> str:
        self._buf += text
        split = len(self._buf)
        i = self._buf.rfind("[")
        if i >= 0 and len(self._buf) - i <= _MAX_PARTIAL and _PARTIAL_RE.fullmatch(self._buf[i:]):
            split = i
        # 인용 앞 공백을 함께 지울 수 있게 끝의 공백도 잡아 둔다
        while split > 0 and self._buf[split - 1].isspace():
            split -= 1
        ready, self._buf = self._buf[:split], self._buf[split:]
        return self._clean(ready)

    def flush(self) -> str:
        out, self._buf = self._clean(self._buf), ""
        return out


class AnswerFilter:
    """추론 제거 → 인용 정리를 차례로 거치는 스트리밍 후처리."""

    def __init__(self, valid_citations: Collection[int]) -> None:
        self.thinking = ThinkingFilter()
        self.citations = CitationFilter(valid_citations)

    def feed(self, text: str) -> str:
        return self.citations.feed(self.thinking.feed(text))

    def flush(self) -> str:
        return self.citations.feed(self.thinking.flush()) + self.citations.flush()


def clean_answer(text: str, valid_citations: Collection[int]) -> str:
    f = AnswerFilter(valid_citations)
    return (f.feed(text) + f.flush()).strip()
