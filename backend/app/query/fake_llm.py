"""질의 그래프용 Fake LLM 응답기 (TRD 14장). `PROVIDER=fake`일 때 팩토리가 붙인다.

시스템 프롬프트의 작업 표식(`app.query.prompts`)을 보고 작업마다 결정적인 답을 만든다.
외부 호출이 없고 같은 입력에는 항상 같은 답이므로,
E2E와 개발 서버에서 그래프 전체 흐름을 확인할 수 있다.

| 작업 | 응답 |
| --- | --- |
| rewrite | 질문을 그대로 쓰는 JSON(요약 낱말이 있으면 intent=summarize) |
| grade | 질문의 Kiwi 토큰(2자 이상) 절반 이상이 들어 있는 자료가 있으면 충분 |
| retry\\_rewrite | 질문 그대로 + 토큰을 이은 키워드 질의 |
| generate | 질문 토큰이 가장 많이 겹치는 자료의 가장 관련 있는 문장 + 그 자료 번호 인용 |
| summarize\\_map / reduce | 자료 앞부분을 잘라 만든 요약 문장 + `[1]` |

오류 재현(E2E·개발): 질문에 `[[fake-error-once:<code>]]`
(code: `rate_limited`, `provider_unavailable`, `provider_auth`)가 있으면 그 질문의 첫 모델
호출에서 해당 Provider 예외를 던진다. 같은 질문을 다시 보내면(다시 시도) 정상으로 답한다.
기억은 프로세스 안에만 둔다. Fake Provider에서만 쓰인다.
"""

import json
import re
from collections.abc import Sequence

from app.ingest.tokenize import bm25_tokens
from app.providers.base import ChatMessage
from app.providers.errors import ProviderAuthError, ProviderError, ProviderUnavailable, RateLimited
from app.query import prompts
from app.query.summary_target import wants_summary

SUFFICIENT_RATIO = 0.5
_FAKE_ERROR_RE = re.compile(r"\[\[fake-error-once:(\w+)\]\]")
_FAKE_ERRORS: dict[str, type[ProviderError]] = {
    "rate_limited": RateLimited,
    "provider_unavailable": ProviderUnavailable,
    "provider_auth": ProviderAuthError,
}
_failed_once: set[str] = set()


def _maybe_fail(question: str) -> None:
    m = _FAKE_ERROR_RE.search(question)
    if m is None or question in _failed_once:
        return
    error = _FAKE_ERRORS.get(m.group(1))
    if error is None:
        return
    _failed_once.add(question)
    raise error(f"fake {m.group(1)}")


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。])\s+|\n+")


def _query_tokens(question: str) -> set[str]:
    tokens = bm25_tokens(question)
    long = {t for t in tokens if len(t) >= 2}
    return long or set(tokens)


def overlap(question: str, text: str) -> float:
    q = _query_tokens(question)
    if not q:
        return 0.0
    return len(q & set(bm25_tokens(text))) / len(q)


def _best_sentence(question: str, text: str) -> str:
    sentences = [s.strip(" #-*\t") for s in _SENTENCE_SPLIT_RE.split(text) if s.strip(" #-*\t")]
    if not sentences:
        return text.strip()
    return max(sentences, key=lambda s: (overlap(question, s), -sentences.index(s)))


def _head(text: str, size: int) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= size else flat[:size] + "…"


def fake_query_responder(messages: Sequence[ChatMessage]) -> str | None:
    task = prompts.task_of(messages)
    if task is None:
        return None
    user = next((m.content for m in reversed(messages) if m.role == "user"), "")
    question = prompts.question_of(user)
    materials = prompts.parse_materials(user)

    _maybe_fail(question)
    question = _FAKE_ERROR_RE.sub("", question).strip()
    if task == prompts.TASK_REWRITE:
        intent = "summarize" if wants_summary(question) else "qa"
        return json.dumps(
            {"question": question, "keyword_query": "", "intent": intent}, ensure_ascii=False
        )
    if task == prompts.TASK_GRADE:
        best = max((overlap(question, body) for body in materials.values()), default=0.0)
        sufficient = best >= SUFFICIENT_RATIO
        reason = "질문 낱말이 자료에 있음" if sufficient else "질문 낱말이 자료에 거의 없음"
        return json.dumps({"sufficient": sufficient, "reason": reason}, ensure_ascii=False)
    if task == prompts.TASK_RETRY:
        keywords = " ".join(sorted(_query_tokens(question)))
        return json.dumps({"query": question, "keyword_query": keywords}, ensure_ascii=False)
    if task == prompts.TASK_GENERATE:
        if not materials:
            return "문서에서 찾지 못했습니다."
        n = max(materials, key=lambda k: (overlap(question, materials[k]), -k))
        return f"자료에 따르면 {_best_sentence(question, materials[n])} [{n}]"
    if task == prompts.TASK_SUMMARIZE_MAP:
        body = next(iter(materials.values()), "")
        return f"부분 요약: {_head(body, 80)}"
    if task == prompts.TASK_SUMMARIZE_REDUCE:
        body = next(iter(materials.values()), "")
        return f"문서 요약: {_head(body, 120)} [1]"
    return None
