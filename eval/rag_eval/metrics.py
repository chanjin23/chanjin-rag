"""평가 지표 (PRD 8장, TRD 14장 평가 CLI 규칙).

| 지표 | 계산 |
| --- | --- |
| Recall@5 | 근거가 있는 문항마다 "상위 5개 청크와 겹친 정답 근거 수 ÷ 정답 근거 수"의 평균 |
| MRR | 정답 근거와 겹치는 첫 청크 순위의 역수 평균(없으면 0) |
| 근거 충실성 | 답한(찾지 못함이 아닌) 문항마다 judge가 준 "인용으로 뒷받침되는 주장 비율"의 평균 |
| 답변 정확도 | 답이 있는 문항마다 judge 판정 정답·부분·오답 = 1·0.5·0의 평균 |
| "찾지 못함" 정확도 | 답 없는(`not_found`) 문항 중 "찾지 못함"으로 답한 비율 |
| p95 응답 시간 | 이번 실행 질의 로그(`query_logs.latency_ms.total`)의 95번째 백분위(nearest-rank) |

겹침 판정은 `(경로, 위치)`로 한다(청크 ID가 아님). 경로는 대소문자·구분자(`/`·`\\`)를 무시하고,
위치는 `location_overlaps`의 규칙을 따른다. 해당 문항이 하나도 없는 지표는 `None`이다.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app.db.keys import make_path_key
from rag_eval.dataset import EvalItem, Evidence

RECALL_K = 5
ANSWER_SCORES: dict[str, float] = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}
# 범위로 비교하는 위치 키. `<키>_end`가 있으면 [키, 키_end] 범위로 본다(청크 위치 형식, TRD 6.3)
RANGE_KEYS = ("page", "slide", "paragraph", "table", "row", "sheet")


def normalize_path(path: str) -> str:
    return make_path_key(path.strip().replace("/", "\\"))


def _range(location: dict[str, Any], key: str) -> tuple[Any, Any] | None:
    if key == "lines":
        value = location.get("lines")
        if isinstance(value, list | tuple) and len(value) == 2:
            return value[0], value[1]
        if isinstance(value, int):
            return value, value
        return None
    if key not in location:
        return None
    start = location[key]
    end = location.get(f"{key}_end", start)
    return start, end


def location_overlaps(label: dict[str, Any], chunk: dict[str, Any]) -> bool:
    """라벨 위치와 청크 위치가 겹치는지.

    - 라벨 위치가 비어 있으면(`{}`) 파일 어디든 인정한다.
    - 아니면 둘 다 가진 위치 키가 하나 이상 있고, 그 키들의 범위가 모두 겹쳐야 한다.
      (`sheet`처럼 숫자가 아닌 값은 같아야 겹친다.)
    """
    if not label:
        return True
    shared = 0
    for key in (*RANGE_KEYS, "lines"):
        a = _range(label, key)
        b = _range(chunk, key)
        if a is None or b is None:
            continue
        shared += 1
        try:
            if a[0] > b[1] or b[0] > a[1]:
                return False
        except TypeError:
            if a != b:
                return False
    return shared > 0


@dataclass(frozen=True)
class RetrievedChunk:
    """검색 결과 한 건(최종 순위, 1부터)."""

    rank: int
    chunk_id: int
    path: str
    location: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "chunk_id": self.chunk_id,
            "path": self.path,
            "location": self.location,
        }


def evidence_matches(evidence: Evidence, chunk: RetrievedChunk) -> bool:
    return normalize_path(evidence.path) == normalize_path(chunk.path) and location_overlaps(
        evidence.location, chunk.location
    )


def recall_at_k(
    evidence: Sequence[Evidence], retrieved: Sequence[RetrievedChunk], k: int = RECALL_K
) -> float:
    if not evidence:
        raise ValueError("정답 근거가 없는 문항은 Recall을 계산하지 않습니다")
    top = sorted(retrieved, key=lambda c: c.rank)[:k]
    found = sum(1 for ev in evidence if any(evidence_matches(ev, c) for c in top))
    return found / len(evidence)


def reciprocal_rank(evidence: Sequence[Evidence], retrieved: Sequence[RetrievedChunk]) -> float:
    for position, chunk in enumerate(sorted(retrieved, key=lambda c: c.rank), start=1):
        if any(evidence_matches(ev, chunk) for ev in evidence):
            return 1.0 / position
    return 0.0


def answer_score(verdict: str) -> float:
    """judge 판정(정답·부분·오답) → 1·0.5·0."""
    try:
        return ANSWER_SCORES[verdict]
    except KeyError:
        raise ValueError(f"알 수 없는 판정: {verdict}") from None


def not_found_accuracy(outcomes: Iterable[tuple[bool, bool]]) -> float | None:
    """(답 없는 문항인가, "찾지 못함"으로 답했는가) 목록 → 답 없는 문항 중 모른다고 답한 비율."""
    expected = [got for expects, got in outcomes if expects]
    if not expected:
        return None
    return sum(1 for got in expected if got) / len(expected)


def p95(values: Iterable[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


# ---------------------------------------------------------------------------
# 라벨 경로 확인 (TRD 5.3: 경로가 documents에 없으면 경고 후 지표에서 뺀다)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelWarning:
    item_id: str
    missing_paths: tuple[str, ...]

    @property
    def message(self) -> str:
        return (
            f"문항 {self.item_id}: 정답 근거 경로 {len(self.missing_paths)}개가 문서 목록에 없어 "
            "지표에서 뺍니다"
        )


def exclude_missing_labels(
    items: Sequence[EvalItem], known_paths: Iterable[str]
) -> tuple[list[EvalItem], list[LabelWarning]]:
    """라벨 경로가 `documents`에 없는 문항을 뺀다. (남은 문항, 경고) 목록을 돌려준다."""
    known = {normalize_path(p) for p in known_paths}
    kept: list[EvalItem] = []
    warnings: list[LabelWarning] = []
    for item in items:
        missing = tuple(ev.path for ev in item.evidence if normalize_path(ev.path) not in known)
        if missing:
            warnings.append(LabelWarning(item.id, missing))
        else:
            kept.append(item)
    return kept, warnings


# ---------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------


@dataclass
class ItemOutcome:
    item_id: str
    type: str
    expects_not_found: bool
    predicted_not_found: bool = False
    recall_at_5: float | None = None
    reciprocal_rank: float | None = None
    verdict: str | None = None
    answer_score: float | None = None
    faithfulness: float | None = None
    latency_ms: float | None = None
    error: str | None = None

    def scores(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "expects_not_found": self.expects_not_found,
            "not_found": self.predicted_not_found,
            "recall_at_5": self.recall_at_5,
            "reciprocal_rank": self.reciprocal_rank,
            "verdict": self.verdict,
            "answer_score": self.answer_score,
            "faithfulness": self.faithfulness,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def summarize(
    outcomes: Sequence[ItemOutcome], *, excluded: int = 0, latencies: Sequence[float] = ()
) -> dict[str, Any]:
    """문항 결과 → `eval_runs.metrics`. 오류가 난 문항은 지표에서 뺀다."""
    ok = [o for o in outcomes if o.error is None]
    answerable = [o for o in ok if not o.expects_not_found]
    return {
        "recall_at_5": _round(mean(o.recall_at_5 for o in answerable)),
        "mrr": _round(mean(o.reciprocal_rank for o in answerable)),
        "faithfulness": _round(mean(o.faithfulness for o in ok if not o.predicted_not_found)),
        "answer_accuracy": _round(mean(o.answer_score for o in answerable)),
        "not_found_accuracy": _round(
            not_found_accuracy((o.expects_not_found, o.predicted_not_found) for o in ok)
        ),
        "p95_ms": _round(p95(latencies)),
        "items": len(outcomes) + excluded,
        "evaluated": len(ok),
        "excluded": excluded,
        "errors": len(outcomes) - len(ok),
    }
