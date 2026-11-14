"""평가 결과 API(`/eval/runs*`, F-13)와 싫어요 피드백 검토 API(`/feedback`, F-12, PRD 8장)."""

import httpx
from chat_seed import parse_sse, seed_corpus
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.db.models import EvalResult, EvalRun, Feedback


async def _run(session: AsyncSession, metrics: dict[str, object], *, finished: bool = True) -> int:
    run = EvalRun(
        dataset_version="set@1",
        config={"judge_model": "fake-judge", "llm_model": "fake-llm"},
        metrics=metrics,
    )
    session.add(run)
    await session.flush()
    if finished:
        run.finished_at = await session.scalar(select(func.now()))
    session.add(EvalResult(run_id=run.id, item_id="q1", answer="답", scores={"recall_at_5": 1.0}))
    await session.flush()
    return run.id


async def test_eval_runs_with_deltas(chat_client: httpx.AsyncClient, session: AsyncSession) -> None:
    first = await _run(session, {"recall_at_5": 0.5, "mrr": 0.5, "p95_ms": 3000.0})
    running = await _run(session, {}, finished=False)  # 끝나지 않은 실행은 비교 기준이 아니다
    latest = await _run(session, {"recall_at_5": 0.75, "mrr": 0.25, "p95_ms": 2500.0})

    with capture_logs() as logs:
        res = await chat_client.get("/api/eval/runs")
    assert res.status_code == 200
    body = res.json()
    assert [m["key"] for m in body["metrics"]][:2] == ["recall_at_5", "mrr"]
    items = {r["id"]: r for r in body["items"]}
    assert [r["id"] for r in body["items"]][:3] == [latest, running, first]
    top = items[latest]
    assert top["previous_id"] == first
    assert top["deltas"]["recall_at_5"] == 0.25
    assert top["deltas"]["mrr"] == -0.25
    assert top["deltas"]["p95_ms"] == -500.0
    assert top["deltas"]["faithfulness"] is None
    assert top["judge_model"] == "fake-judge"
    assert any(r["event"] == "eval.runs.listed" for r in logs)

    res = await chat_client.get(f"/api/eval/runs/{latest}")
    assert res.status_code == 200
    detail = res.json()
    assert detail["previous_id"] == first
    assert detail["previous_metrics"]["recall_at_5"] == 0.5
    assert detail["results"][0]["item_id"] == "q1"
    assert detail["config"]["judge_model"] == "fake-judge"

    res = await chat_client.get("/api/eval/runs/999999")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "eval_run_not_found"


async def test_feedback_review_sets_failure_type(
    chat_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed_corpus(session)
    res = await chat_client.post(
        "/api/conversations/new/messages", json={"text": "위약금은 얼마야"}
    )
    message_id = int([d for n, d in parse_sse(res.text) if n == "done"][0]["message_id"])
    res = await chat_client.put(
        f"/api/messages/{message_id}/feedback",
        json={"rating": -1, "reason": "wrong", "note": "틀린 답"},
    )
    assert res.status_code == 200

    res = await chat_client.get("/api/feedback", params={"rating": -1})
    assert res.status_code == 200
    body = res.json()
    item = next(i for i in body["items"] if i["message_id"] == message_id)
    assert item["question"] == "위약금은 얼마야"
    assert item["answer"]
    assert item["reason"] == "wrong" and item["note"] == "틀린 답"
    assert item["failure_type"] is None and item["reviewed_at"] is None
    assert body["unreviewed"] >= 1

    with capture_logs() as logs:
        res = await chat_client.patch(
            f"/api/feedback/{item['id']}", json={"failure_type": "retrieval"}
        )
    assert res.status_code == 200
    assert res.json()["failure_type"] == "retrieval"
    assert res.json()["reviewed_at"]
    reviewed = [r for r in logs if r["event"] == "feedback.reviewed"]
    assert reviewed and reviewed[0]["failure_type"] == "retrieval"
    assert reviewed[0]["message_id"] == message_id
    fb = await session.scalar(select(Feedback).where(Feedback.id == item["id"]))
    assert fb is not None
    await session.refresh(fb)
    assert fb.reviewed_at is not None

    # 다시 피드백을 보내도 검토 결과는 남는다
    await chat_client.put(f"/api/messages/{message_id}/feedback", json={"rating": -1})
    res = await chat_client.get("/api/feedback", params={"rating": -1})
    again = next(i for i in res.json()["items"] if i["message_id"] == message_id)
    assert again["failure_type"] == "retrieval" and again["reviewed_at"]

    # 좋아요만 보면 이 항목은 없다
    res = await chat_client.get("/api/feedback", params={"rating": 1})
    assert all(i["message_id"] != message_id for i in res.json()["items"])

    assert (
        await chat_client.patch("/api/feedback/999999", json={"failure_type": None})
    ).status_code == 404
    bad = await chat_client.patch(f"/api/feedback/{item['id']}", json={"failure_type": "x"})
    assert bad.status_code == 422
