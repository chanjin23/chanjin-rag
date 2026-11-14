"""LLM-as-judge (PRD 8장, TRD 14장).

- 답변 정확도: 정답과 비교해 `correct`(정답)·`partial`(부분)·`wrong`(오답) 중 하나.
- 근거 충실성: 답변의 주장 중 인용 자료로 뒷받침되는 수 ÷ 전체 주장 수.

판정 모델은 답변 모델과 달라야 한다(같은 모델은 점수를 후하게 줄 수 있음). `EVAL_JUDGE_MODEL`,
키는 `NVIDIA_JUDGE_API_KEY`. `PROVIDER=fake`면 결정적인 Fake 판정기(`fake_judge_responder`)를 쓴다.
판정 응답을 읽지 못하면 그 문항의 해당 점수는 `None`(지표에서 빠짐)이다.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings
from app.ingest.tokenize import bm25_tokens
from app.providers.base import ChatMessage, LLMProvider
from app.providers.errors import ProviderConfigError
from app.providers.fake import FakeLLMProvider
from app.query.prompts import parse_json_object

FAKE_JUDGE_MODEL = "fake-judge"
TASK_ACCURACY = "[판정:accuracy]"
TASK_FAITHFULNESS = "[판정:faithfulness]"
VERDICTS = ("correct", "partial", "wrong")
JUDGE_MAX_TOKENS = 400
# 판정에 넣는 자료 한 개의 최대 글자 수
MATERIAL_CHARS = 2000

_CITATION_RE = re.compile(r"\[\d+\]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。])\s+|\n+")


class JudgeConfigError(ValueError):
    """판정 모델 설정 오류(답변 모델과 같음 등)."""


def accuracy_messages(question: str, expected: str, answer: str) -> list[ChatMessage]:
    system = "\n".join(
        [
            TASK_ACCURACY,
            "질문, 정답, 평가할 답변이 JSON으로 주어집니다. 답변이 정답과 일치하는지 판정하세요.",
            '정답의 핵심을 모두 맞혔으면 "correct", 일부만 맞혔거나 빠진 것이 있으면 "partial",',
            '틀렸거나 답하지 못했으면 "wrong"입니다.',
            'JSON 한 개로만 답하세요: {"verdict": "correct|partial|wrong", "reason": "짧은 이유"}',
        ]
    )
    user = json.dumps(
        {"question": question, "expected": expected, "answer": answer}, ensure_ascii=False
    )
    return [ChatMessage("system", system), ChatMessage("user", user)]


def faithfulness_messages(answer: str, materials: Sequence[str]) -> list[ChatMessage]:
    system = "\n".join(
        [
            TASK_FAITHFULNESS,
            "답변과 답변이 인용한 자료가 JSON으로 주어집니다.",
            "답변을 사실 주장 단위로 나누고, 각 주장이 자료로 뒷받침되는지 판정하세요.",
            'JSON 한 개로만 답하세요: {"supported": 뒷받침되는 주장 수, "total": 전체 주장 수}',
        ]
    )
    user = json.dumps(
        {"answer": answer, "materials": [m[:MATERIAL_CHARS] for m in materials]},
        ensure_ascii=False,
    )
    return [ChatMessage("system", system), ChatMessage("user", user)]


def parse_verdict(reply: str) -> str | None:
    parsed = parse_json_object(reply)
    if parsed is None:
        return None
    verdict = str(parsed.get("verdict") or "").strip().lower()
    return verdict if verdict in VERDICTS else None


def parse_faithfulness(reply: str) -> float | None:
    parsed = parse_json_object(reply)
    if parsed is None:
        return None
    supported, total = parsed.get("supported"), parsed.get("total")
    if not isinstance(supported, int | float) or not isinstance(total, int | float):
        return None
    if total <= 0:
        return None
    return max(0.0, min(1.0, float(supported) / float(total)))


# ---------------------------------------------------------------------------
# Fake 판정기 (테스트·E2E)
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    tokens = bm25_tokens(_CITATION_RE.sub(" ", text))
    long = {t for t in tokens if len(t) >= 2}
    return long or set(tokens)


def _overlap(reference: str, text: str) -> float:
    ref = _tokens(reference)
    if not ref:
        return 0.0
    return len(ref & _tokens(text)) / len(ref)


def fake_judge_responder(messages: Sequence[ChatMessage]) -> str | None:
    """정답 낱말이 답변에 80% 이상 있으면 정답, 40% 이상이면 부분. 답변 문장은 자료와 낱말이
    절반 이상 겹치면 뒷받침된다고 본다."""
    system = next((m.content for m in messages if m.role == "system"), "")
    user = next((m.content for m in reversed(messages) if m.role == "user"), "")
    try:
        data: dict[str, Any] = json.loads(user)
    except ValueError:
        return None
    if system.startswith(TASK_ACCURACY):
        score = _overlap(str(data.get("expected", "")), str(data.get("answer", "")))
        verdict = "correct" if score >= 0.8 else "partial" if score >= 0.4 else "wrong"
        return json.dumps({"verdict": verdict, "reason": f"낱말 겹침 {score:.2f}"})
    if system.startswith(TASK_FAITHFULNESS):
        answer = _CITATION_RE.sub(" ", str(data.get("answer", "")))
        claims = [s.strip() for s in _SENTENCE_SPLIT_RE.split(answer) if s.strip()]
        materials = [str(m) for m in data.get("materials") or []]
        supported = sum(1 for c in claims if any(_overlap(c, m) >= 0.5 for m in materials))
        return json.dumps({"supported": supported, "total": len(claims)})
    return None


def create_judge_provider(settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    if s.provider == "fake":
        return FakeLLMProvider(model=FAKE_JUDGE_MODEL, responder=fake_judge_responder)
    if s.nvidia_judge_api_key is None:
        raise ProviderConfigError("NVIDIA_JUDGE_API_KEY가 설정되지 않았습니다")
    from app.providers.nvidia import NvidiaLLMProvider

    return NvidiaLLMProvider(
        api_key=s.nvidia_judge_api_key.get_secret_value(),
        model=s.eval_judge_model,
        base_url=s.nvidia_base_url,
        temperature=0.0,
        max_tokens=JUDGE_MAX_TOKENS,
        read_timeout=s.llm_read_timeout,
    )


@dataclass
class Judge:
    llm: LLMProvider

    @property
    def model(self) -> str:
        return self.llm.model

    async def accuracy(self, question: str, expected: str, answer: str) -> str | None:
        reply = await self.llm.complete(
            accuracy_messages(question, expected, answer),
            temperature=0.0,
            max_tokens=JUDGE_MAX_TOKENS,
        )
        return parse_verdict(reply)

    async def faithfulness(self, answer: str, materials: Sequence[str]) -> float | None:
        if not materials:
            return 0.0
        reply = await self.llm.complete(
            faithfulness_messages(answer, materials),
            temperature=0.0,
            max_tokens=JUDGE_MAX_TOKENS,
        )
        return parse_faithfulness(reply)


def ensure_distinct_models(answer_model: str, judge_model: str) -> None:
    if answer_model.strip().lower() == judge_model.strip().lower():
        raise JudgeConfigError(
            f"판정 모델({judge_model})이 답변 모델과 같습니다. EVAL_JUDGE_MODEL을 다르게 설정하세요"
        )
