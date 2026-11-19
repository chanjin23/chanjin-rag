"""한국어 BM25 토큰화 (Kiwi, TRD 6.4).

문서 청크(`chunks.bm25_text`)와 질문이 **같은 함수**(`bm25_tokens`, 문자열은 `bm25_text`)를 쓴다.
토큰화가 어긋나면 BM25 품질이 바로 떨어지므로 다른 토큰화 경로를 만들지 않는다.

- 남기는 품사: 명사(NNG, NNP, NNB), 동사·형용사 어간(VV, VA), 외국어(SL), 숫자(SN),
  한자(SH), 어근(XR).
  조사·어미·기호 등은 버린다. Kiwi 태그의 불규칙 표시(`VA-I` 등)는 `-` 앞만 본다.
- 영문은 소문자로 바꾼다.
- 복합명사: 띄어쓰기 없이 붙어 나온 일반·고유명사(NNG, NNP) 연속은 원형과 분해형을 모두 넣는다
  ("위약금조항" → `위약금조항 위약금 조항`).
- URL·이메일·날짜 같은 패턴은 Kiwi가 한 토큰(W_URL 등)으로 묶어 버리게 되므로 그 매칭을 끄고
  SL·SN 조각으로 남긴다("2024-01-02" → `2024 01 02`).
- 사용자 사전: `KIWI_USER_DICT_PATH`(기본 `deploy/kiwi_user_dict.txt`).
  Kiwi 형식(`단어<TAB>품사[<TAB>점수]`, `#` 주석). 사전에 넣은 단어는 나누지 않는다.
  사전을 바꾸면 `backend reindex --bm25`로 다시 색인한다.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from kiwipiepy import Kiwi, Match

from app.core.config import REPO_ROOT, get_settings

DEFAULT_USER_DICT = REPO_ROOT / "deploy" / "kiwi_user_dict.txt"

KEEP_TAGS = frozenset({"NNG", "NNP", "NNB", "VV", "VA", "SL", "SN", "SH", "XR"})
COMPOUND_TAGS = frozenset({"NNG", "NNP"})

_PATTERN_MATCHES = Match.URL | Match.EMAIL | Match.SERIAL | Match.HASHTAG | Match.MENTION
_MATCH_OPTIONS = Match.ALL & ~_PATTERN_MATCHES


class KiwiTokenizer:
    """Kiwi 인스턴스 하나를 감싼다. 직접 만들기보다 `get_tokenizer()`나 `bm25_tokens()`를 쓴다."""

    def __init__(self, user_dict: Path | None = None) -> None:
        self._kiwi: Any = Kiwi()
        self.user_dict = user_dict
        self.user_words = 0
        if user_dict is not None and user_dict.is_file():
            self.user_words = int(self._kiwi.load_user_dictionary(str(user_dict)))

    def tokens(self, text: str) -> list[str]:
        if not text.strip():
            return []
        out: list[str] = []
        run: list[tuple[str, int]] = []  # 붙어 있는 복합명사 후보 (형태, 끝 위치)

        def flush() -> None:
            if len(run) >= 2:
                out.append("".join(form for form, _ in run))
            out.extend(form for form, _ in run)
            run.clear()

        for tok in self._kiwi.tokenize(text, match_options=_MATCH_OPTIONS):
            tag = str(tok.tag).split("-", 1)[0]
            if tag not in KEEP_TAGS:
                flush()
                continue
            form = str(tok.form).lower()
            if tag in COMPOUND_TAGS:
                if run and run[-1][1] != tok.start:
                    flush()
                run.append((form, int(tok.end)))
                continue
            flush()
            out.append(form)
        flush()
        return out


def resolve_user_dict() -> Path | None:
    configured = get_settings().kiwi_user_dict_path
    if configured:
        return Path(configured)
    return DEFAULT_USER_DICT


@lru_cache(maxsize=4)
def _tokenizer_for(user_dict: Path | None) -> KiwiTokenizer:
    return KiwiTokenizer(user_dict)


def get_tokenizer() -> KiwiTokenizer:
    """설정의 사용자 사전을 읽은 토크나이저(프로세스마다 사전 경로별로 하나)."""
    return _tokenizer_for(resolve_user_dict())


def bm25_tokens(text: str) -> list[str]:
    """문서와 질문 공용 BM25 토큰화 함수(TRD 6.4). 이 함수 하나만 쓴다."""
    return get_tokenizer().tokens(text)


def bm25_text(text: str) -> str:
    """`chunks.bm25_text`와 질문 검색어로 쓰는 공백 구분 토큰 문자열."""
    return " ".join(bm25_tokens(text))
