"""평가 지표 (PRD 8장, TRD 14장): (경로, 위치) 겹침, 찾지 못함 정확도, 판정 점수, 라벨 경로 확인."""

import pytest

from rag_eval.dataset import EvalItem, Evidence, parse_dataset
from rag_eval.judge import (
    accuracy_messages,
    faithfulness_messages,
    fake_judge_responder,
    parse_faithfulness,
    parse_verdict,
)
from rag_eval.metrics import (
    ItemOutcome,
    RetrievedChunk,
    answer_score,
    exclude_missing_labels,
    location_overlaps,
    not_found_accuracy,
    p95,
    recall_at_k,
    reciprocal_rank,
    summarize,
)

DOC_A = r"C:\docs\계약\계약서.pdf"
DOC_B = r"C:\docs\회의\회의록.txt"


def chunk(rank: int, path: str, location: dict[str, object]) -> RetrievedChunk:
    return RetrievedChunk(rank=rank, chunk_id=rank * 10, path=path, location=location)


def test_recall_and_mrr_with_location_overlap() -> None:
    # 위치 범위 겹침: 청크 3~4쪽은 라벨 4쪽과 겹치고 5쪽과는 겹치지 않는다
    assert location_overlaps({"page": 4}, {"page": 3, "page_end": 4})
    assert not location_overlaps({"page": 5}, {"page": 3, "page_end": 4})
    assert location_overlaps({"lines": [10, 12]}, {"lines": [12, 20]})
    assert not location_overlaps({"lines": [1, 5]}, {"lines": [6, 9]})
    # 라벨 위치가 비면 파일 어디든, 공통 위치 키가 없으면 겹치지 않음
    assert location_overlaps({}, {"slide": 3})
    assert not location_overlaps({"page": 1}, {"slide": 1})

    evidence = [Evidence(DOC_A, {"page": 4}), Evidence(DOC_B, {"lines": [3, 3]})]
    retrieved = [
        chunk(1, DOC_A, {"page": 1}),  # 같은 파일, 다른 위치
        chunk(2, r"C:\docs\기타.md", {"lines": [3, 3]}),  # 다른 파일, 같은 위치
        # 경로는 대소문자·구분자를 무시한다
        chunk(3, "c:/DOCS/계약/계약서.pdf", {"page": 3, "page_end": 4}),
        chunk(4, DOC_A, {"page": 9}),
        chunk(5, DOC_A, {"page": 10}),
        chunk(6, DOC_B, {"lines": [1, 4]}),  # 6위: 상위 5개 밖
    ]
    assert recall_at_k(evidence, retrieved) == pytest.approx(0.5)
    assert recall_at_k(evidence, retrieved, k=6) == pytest.approx(1.0)
    assert reciprocal_rank(evidence, retrieved) == pytest.approx(1 / 3)
    assert reciprocal_rank(evidence, retrieved[:2]) == 0.0

    outcomes = [
        ItemOutcome("a", "single", False, recall_at_5=0.5, reciprocal_rank=1 / 3),
        ItemOutcome("b", "single", False, recall_at_5=1.0, reciprocal_rank=1.0),
        # 답 없는 문항은 Recall·MRR에서 빠진다
        ItemOutcome("c", "not_found", True, predicted_not_found=True),
    ]
    metrics = summarize(outcomes)
    assert metrics["recall_at_5"] == pytest.approx(0.75)
    assert metrics["mrr"] == pytest.approx(round((1 / 3 + 1.0) / 2, 4))


def test_not_found_accuracy() -> None:
    # (답 없는 문항인가, 찾지 못함으로 답했는가)
    outcomes = [(True, True), (True, False), (True, True), (True, True), (False, True)]
    assert not_found_accuracy(outcomes) == pytest.approx(0.75)
    assert not_found_accuracy([(False, False)]) is None

    items = [
        ItemOutcome("n1", "not_found", True, predicted_not_found=True),
        ItemOutcome("n2", "not_found", True, predicted_not_found=False),
        ItemOutcome("s1", "single", False, predicted_not_found=True, answer_score=0.0),
        # 오류 난 문항은 지표에서 빠진다
        ItemOutcome("n3", "not_found", True, error="ProviderUnavailable"),
    ]
    metrics = summarize(items, latencies=[100.0, 200.0, 9000.0])
    assert metrics["not_found_accuracy"] == pytest.approx(0.5)
    assert metrics["errors"] == 1
    assert metrics["evaluated"] == 3
    assert metrics["p95_ms"] == 9000.0
    assert p95([float(i) for i in range(1, 101)]) == 95.0
    assert p95([]) is None


def test_answer_score_levels() -> None:
    assert answer_score("correct") == 1.0
    assert answer_score("partial") == 0.5
    assert answer_score("wrong") == 0.0
    with pytest.raises(ValueError):
        answer_score("maybe")

    # judge 응답 파싱: 코드 블록이 붙어도 읽고, 모르는 판정은 None
    assert parse_verdict('```json\n{"verdict": "Partial", "reason": "x"}\n```') == "partial"
    assert parse_verdict('{"verdict": "unknown"}') is None
    assert parse_faithfulness('{"supported": 3, "total": 4}') == pytest.approx(0.75)
    assert parse_faithfulness('{"supported": 1, "total": 0}') is None

    # Fake 판정기: 정답 낱말이 모두 있으면 정답, 일부면 부분, 없으면 오답
    def verdict(expected: str, answer: str) -> str | None:
        reply = fake_judge_responder(accuracy_messages("질문", expected, answer))
        assert reply is not None
        return parse_verdict(reply)

    full = "자료에 따르면 위약금은 계약 금액의 10%이다 [1]"
    assert verdict("위약금은 계약 금액의 10%이다", full) == "correct"
    assert verdict("담당자는 기획팀이고 배포 담당은 개발팀", "담당자는 기획팀이다") == "partial"
    assert verdict("위약금은 계약 금액의 10%", "문서에서 찾지 못했습니다.") == "wrong"

    reply = fake_judge_responder(
        faithfulness_messages(
            "위약금은 계약 금액의 10%이다 [1]. 화성 탐사선 연료는 충분하다 [1].",
            ["위약금은 계약 금액의 10%로 한다."],
        )
    )
    assert reply is not None
    assert parse_faithfulness(reply) == pytest.approx(0.5)

    # 정확도 평균: 정답·부분·오답 = 1·0.5·0
    outcomes = [
        ItemOutcome("a", "single", False, answer_score=answer_score("correct")),
        ItemOutcome("b", "single", False, answer_score=answer_score("partial")),
        ItemOutcome("c", "multi", False, answer_score=answer_score("wrong")),
    ]
    assert summarize(outcomes)["answer_accuracy"] == pytest.approx(0.5)


def test_missing_label_path_excluded_with_warning() -> None:
    dataset = parse_dataset(
        f"""
version: 3
items:
  - id: ok
    type: single
    question: 위약금은?
    answer: 10%
    evidence:
      - path: '{DOC_A}'
        location: {{page: 4}}
  - id: moved
    type: multi
    question: 회의와 계약?
    answer: 둘 다
    evidence:
      - path: '{DOC_A}'
      - path: 'C:\\docs\\옮긴파일.md'
  - id: none
    type: not_found
    question: 화성 탐사선 연료는?
""",
        name="sample",
    )
    assert dataset.label.startswith("sample@3+")
    kept, warnings = exclude_missing_labels(dataset.items, [DOC_A.upper(), DOC_B])
    assert [i.id for i in kept] == ["ok", "none"]
    assert len(warnings) == 1
    assert warnings[0].item_id == "moved"
    assert warnings[0].missing_paths == ("C:\\docs\\옮긴파일.md",)
    assert "moved" in warnings[0].message and "지표에서 뺍니다" in warnings[0].message

    # 제외한 문항 수는 실행 지표에 남는다
    outcomes = [ItemOutcome(i.id, i.type, i.expects_not_found) for i in kept]
    metrics = summarize(outcomes, excluded=len(warnings))
    assert metrics["items"] == 3
    assert metrics["excluded"] == 1


def test_dataset_rejects_bad_items() -> None:
    from rag_eval.dataset import DatasetError

    with pytest.raises(DatasetError, match="type"):
        parse_dataset("items:\n  - {id: a, question: q, type: weird, answer: x}\n")
    with pytest.raises(DatasetError, match="evidence"):
        parse_dataset("items:\n  - {id: a, question: q, type: single, answer: x}\n")
    with pytest.raises(DatasetError, match="겹칩니다"):
        parse_dataset(
            "items:\n  - {id: a, question: q, type: not_found}\n"
            "  - {id: a, question: r, type: not_found}\n"
        )
    item = EvalItem("x", "q", "hwp", "a", (Evidence(DOC_A),))
    assert not item.expects_not_found
