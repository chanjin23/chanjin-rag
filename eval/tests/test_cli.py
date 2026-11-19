"""`rag-eval run`·`compare` (F-13, TRD 5.3, 14장).

테스트 DB 세션(롤백)과 Fake Provider로 실행한다.
"""

from pathlib import Path

import pytest
from chat_seed import fake_embedder, fake_llm, fake_reranker, seed_corpus
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EvalResult, EvalRun, QueryLog
from rag_eval.cli import compare_runs, run_dataset
from rag_eval.judge import JudgeConfigError, create_judge_provider
from rag_eval.runner import EvalProviders

DATASET = r"""
version: 2
items:
  - id: contract
    type: single
    question: 위약금은 계약 금액의 몇 %인가?
    answer: 위약금은 계약 금액의 10%로 한다.
    evidence:
      - path: 'C:\docs\계약\계약서.md'
        location: {lines: [1, 1]}
  - id: project
    type: single
    question: 알파 프로젝트의 마감일은 언제인가?
    answer: 알파 프로젝트의 마감일은 12월 15일이다.
    evidence:
      - path: 'c:/docs/프로젝트/알파.md'
  - id: none
    type: not_found
    question: 화성 탐사선 연료 탱크 용량은?
  - id: moved
    type: single
    question: 옮긴 문서의 내용은?
    answer: 없음
    evidence:
      - path: 'C:\docs\옮김\사라진.md'
"""


def providers(judge_model: str | None = None) -> EvalProviders:
    judge = create_judge_provider()
    if judge_model is not None:
        judge.model = judge_model
    return EvalProviders(
        llm=fake_llm(), embedder=fake_embedder(), reranker=fake_reranker(), judge=judge
    )


async def test_run_with_fake_providers_saves_run(session: AsyncSession, tmp_path: Path) -> None:
    await seed_corpus(session)
    await session.commit()
    path = tmp_path / "set.yaml"
    path.write_text(DATASET, encoding="utf-8")
    out: list[str] = []

    report = await run_dataset(session, path, providers(), git_sha="abc123", echo=out.append)

    run = await session.get(EvalRun, report.run.id)
    assert run is not None and run.finished_at is not None
    assert run.dataset_version.startswith("set@2+")
    assert run.git_sha == "abc123"
    # 판정 모델은 답변 모델과 다르고 config에 기록된다
    assert run.config["judge_model"] == "fake-judge"
    assert run.config["llm_model"] != run.config["judge_model"]
    assert run.config["excluded_items"] == ["moved"]
    assert run.config["embed_quota"] == "not_counted"

    m = run.metrics
    assert m["items"] == 4 and m["evaluated"] == 3 and m["excluded"] == 1 and m["errors"] == 0
    assert m["recall_at_5"] == pytest.approx(1.0)
    assert m["mrr"] == pytest.approx(1.0)
    assert m["not_found_accuracy"] == pytest.approx(1.0)
    assert m["answer_accuracy"] == pytest.approx(1.0)
    assert m["faithfulness"] == pytest.approx(1.0)
    assert m["p95_ms"] is not None and m["p95_ms"] > 0

    results = {
        r.item_id: r
        for r in (
            await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))
        ).scalars()
    }
    assert set(results) == {"contract", "project", "none", "moved"}
    assert results["moved"].scores["excluded"] is True
    contract = results["contract"]
    assert contract.scores["verdict"] == "correct"
    assert contract.retrieved[0]["path"] == r"C:\docs\계약\계약서.md"
    assert "10%" in (contract.answer or "")
    assert results["none"].scores["not_found"] is True

    # 평가 질의는 질의 로그에 source='eval'로 남는다
    log_ids = [r.scores["query_log_id"] for r in results.values() if "query_log_id" in r.scores]
    assert len(log_ids) == 3
    sources = (
        await session.execute(select(QueryLog.source).where(QueryLog.id.in_(log_ids)))
    ).scalars()
    assert set(sources) == {"eval"}

    # 경고와 지표가 출력된다
    text = "\n".join(out)
    assert "경고: 문항 moved" in text
    assert "Recall@5" in text and "1.000" in text


async def test_run_rejects_same_judge_model(session: AsyncSession, tmp_path: Path) -> None:
    path = tmp_path / "set.yaml"
    path.write_text(DATASET, encoding="utf-8")
    before = await session.scalar(select(func.count()).select_from(EvalRun))
    with pytest.raises(JudgeConfigError):
        await run_dataset(session, path, providers(judge_model="fake-llm"), echo=lambda _: None)
    assert await session.scalar(select(func.count()).select_from(EvalRun)) == before


async def test_compare_runs_shows_deltas(session: AsyncSession) -> None:
    base = EvalRun(
        dataset_version="set@1",
        config={"judge_model": "fake-judge"},
        metrics={
            "recall_at_5": 0.5,
            "mrr": 0.4,
            "faithfulness": 0.9,
            "answer_accuracy": 0.75,
            "not_found_accuracy": None,
            "p95_ms": 2000.0,
        },
    )
    target = EvalRun(
        dataset_version="set@2",
        config={"judge_model": "fake-judge"},
        metrics={
            "recall_at_5": 0.75,
            "mrr": 0.4,
            "faithfulness": 0.8,
            "answer_accuracy": 1.0,
            "not_found_accuracy": 1.0,
            "p95_ms": 1500.0,
        },
    )
    session.add_all([base, target])
    await session.flush()

    lines = await compare_runs(session, base.id, target.id)
    text = "\n".join(lines)
    assert f"#{base.id}" in lines[0] and f"#{target.id}" in lines[0]
    row = {line.split()[0]: line.split() for line in lines[2:]}
    assert row["Recall@5"][-3:] == ["0.500", "0.750", "+0.250"]
    assert row["MRR"][-1] == "+0.000"
    assert lines[4].split()[-1] == "-0.100"  # 근거 충실성
    assert lines[5].split()[-1] == "+0.250"  # 답변 정확도
    assert lines[6].split()[-3:] == ["-", "1.000", "-"]  # 이전 값이 없으면 증감 없음
    assert lines[7].split()[-1] == "-500"  # p95(ms)
    assert "증감" in text

    from rag_eval.cli import CompareError

    with pytest.raises(CompareError):
        await compare_runs(session, base.id, 999_999)
