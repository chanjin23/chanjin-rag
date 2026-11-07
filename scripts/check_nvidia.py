"""NVIDIA API 키와 모델 3종(LLM, 임베딩, 재정렬)이 실제로 동작하는지 확인한다 (TRD 13장 2·3번).

실행: 저장소 루트에서 `uv run python scripts/check_nvidia.py`
  --only llm|embed|rerank   하나만 확인
  --runs N                  임베딩·재정렬 지연 시간을 N번 재서 중앙값(기본 3)
  --llm-runs N              LLM 반복 횟수(기본 1)
  --out PATH                결과를 JSON으로 저장

설정은 환경 변수 → 저장소 루트 `.env` → `backend/.env` 순서로 읽는다.
API 키는 모델마다 따로 쓴다: NVIDIA_LLM_API_KEY, NVIDIA_EMBED_API_KEY, NVIDIA_RERANK_API_KEY.
키가 없는 모델은 건너뛰고 "키 없음"으로 표시한다. 키는 화면과 파일 어디에도 출력하지 않는다.
테스트 문장은 고정된 예시 문장만 보낸다(내 문서는 보내지 않음).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "NVIDIA_BASE_URL": "https://integrate.api.nvidia.com/v1",
    "LLM_MODEL": "nvidia/nemotron-3.5-lightning-30b-a3b",
    "EMBED_MODEL": "nvidia/nemotron-3.5-embed-8b",
    "EMBED_DIM": "2048",
    "RERANK_MODEL": "nvidia/nemotron-3.5-rerank-8b",
    "RERANK_URL": "https://ai.api.nvidia.com/v1/retrieval/nvidia/nemotron-3.5-rerank-8b/reranking",
    # 모델별 추가 요청값(JSON). 추론을 끄는 키가 모델마다 다르다(.env.example 참고)
    "LLM_EXTRA_BODY": '{"chat_template_kwargs":{"enable_thinking":false}}',
}
# 모델마다 발급받은 키가 다르므로 키 이름도 모델별로 둔다(.env.example 참고)
KEY_VARS = {
    "llm": "NVIDIA_LLM_API_KEY",
    "embed": "NVIDIA_EMBED_API_KEY",
    "rerank": "NVIDIA_RERANK_API_KEY",
}
LABELS = {
    "llm": ("LLM", "LLM_MODEL"),
    "embed": ("임베딩", "EMBED_MODEL"),
    "rerank": ("재정렬", "RERANK_MODEL"),
}
HALFVEC_HNSW_MAX = 4000  # pgvector halfvec HNSW 색인 최대 차원 (TRD 6.3)

QUERY = "계약을 중도 해지하면 위약금은 얼마인가요?"
PASSAGES = [
    "제12조(위약금) 고객이 계약 기간 중 해지하면 잔여 계약 금액의 10%를 위약금으로 지급한다.",
    "회의실 예약은 사내 포털에서 하루 전까지 신청해야 하며, 당일 취소는 불가합니다.",
    "2026년 3분기 서버 비용은 전 분기 대비 12% 감소했다.",
]


# --------------------------------------------------------------------------- 설정


def load_env() -> dict[str, str]:
    values = dict(DEFAULTS)
    for path in (ROOT / "backend" / ".env", ROOT / ".env"):  # 뒤에 읽은 루트 .env가 우선
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                value = m.group(2).strip().strip('"').strip("'")
                if value:
                    values[m.group(1)] = value
    for key in (*DEFAULTS, *KEY_VARS.values()):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def say(msg: str) -> None:
    print(msg, flush=True)


def mask(key: str) -> str:
    return f"{key[:6]}…({len(key)}자)" if len(key) > 10 else "(너무 짧음)"


def auth(env: dict[str, str], kind: str) -> dict[str, str]:
    """모델 종류(llm, embed, rerank)에 맞는 키로 인증 헤더를 만든다."""
    return {"Authorization": f"Bearer {env[KEY_VARS[kind]]}"}


# --------------------------------------------------------------------------- 결과


@dataclass
class Check:
    name: str
    ok: bool = False
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def explain_http_error(resp: httpx.Response) -> str:
    hints = {
        401: "API 키가 틀렸거나 만료됐습니다. build.nvidia.com에서 키를 다시 발급하세요.",
        # NVIDIA는 틀린 키에도 401이 아니라 403 "Authorization failed"를 돌려준다
        403: "인증 실패. 키가 틀렸거나 만료됐거나, 이 모델에 접근 권한이 없습니다.",
        404: "모델 ID나 URL이 틀렸습니다. 모델 페이지의 API 예제와 비교하세요.",
        422: "요청 형식이 맞지 않습니다. 응답 본문의 오류 필드를 확인하세요.",
        429: "호출 한도에 걸렸습니다. 잠시 뒤 다시 실행하세요.",
    }
    body = resp.text[:400].replace("\n", " ")
    return f"HTTP {resp.status_code}: {hints.get(resp.status_code, '서버 오류')} | 응답: {body}"


def rate_headers(resp: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items() if "ratelimit" in k.lower() or k == "retry-after"}


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


# --------------------------------------------------------------------------- 확인 항목


def check_models(client: httpx.Client, env: dict[str, str]) -> Check:
    c = Check("모델 목록")
    resp = client.get(f"{env['NVIDIA_BASE_URL']}/models")
    if resp.status_code != 200:
        c.error = explain_http_error(resp)
        return c
    ids = {m.get("id") for m in resp.json().get("data", [])}
    found = {name: env[name] in ids for name in ("LLM_MODEL", "EMBED_MODEL")}
    c.details = {"models_listed": len(ids), "found": found}
    if not found["LLM_MODEL"]:
        family = env["LLM_MODEL"].split("/")[0]
        c.details["same_vendor_models"] = sorted(i for i in ids if i and i.startswith(family))
    c.ok = all(found.values())
    c.summary = ", ".join(f"{env[k]} {'있음' if v else '없음'}" for k, v in found.items())
    c.summary += " (인증 없이 보이는 목록이라 키 검증은 아래 항목에서 함)"
    return c


def check_llm(client: httpx.Client, env: dict[str, str], runs: int) -> Check:
    """스트리밍 한 번으로 첫 글자 시간, 전체 시간, 추론 텍스트 여부를 함께 잰다(TRD 7.4).

    무료 엔드포인트는 대기열 때문에 응답 시작까지 1분 넘게 걸리기도 해서 기본 1회만 잰다.
    `LLM_EXTRA_BODY`(JSON)를 요청 본문에 그대로 합친다. 기본값은 nemotron의 추론 끄기다.
    """
    c = Check("LLM")
    url = f"{env['NVIDIA_BASE_URL']}/chat/completions"
    body: dict[str, Any] = {
        "model": env["LLM_MODEL"],
        "messages": [
            {"role": "system", "content": "한국어로 한 문장으로만 답하세요."},
            {"role": "user", "content": "RAG가 무엇인지 설명해 주세요."},
        ],
        "temperature": 0.2,
        "max_tokens": 256,
        "stream": True,
    }
    extra = env.get("LLM_EXTRA_BODY", "").strip()
    if extra:
        try:
            body.update(json.loads(extra))
        except json.JSONDecodeError as e:
            c.error = f"LLM_EXTRA_BODY가 올바른 JSON이 아닙니다: {e}"
            return c

    header_s: list[float] = []
    first_s: list[float] = []
    total_s: list[float] = []
    content = ""
    reasoning = ""
    for i in range(runs):
        say(f"  LLM 요청 {i + 1}/{runs} (모델 대기열이 길면 1분 넘게 걸리기도 합니다)")
        start = time.perf_counter()
        first: float | None = None
        content = reasoning = ""
        with client.stream("POST", url, json=body, headers=auth(env, "llm")) as s:
            header_s.append(time.perf_counter() - start)
            if s.status_code != 200:
                s.read()
                c.error = explain_http_error(s)
                return c
            for line in s.iter_lines():
                if not line.startswith("data:") or line.strip() == "data: [DONE]":
                    continue
                choices = json.loads(line[5:]).get("choices") or [{}]
                delta = choices[0].get("delta", {})
                reasoning += delta.get("reasoning_content") or ""
                if delta.get("content"):
                    first = first or time.perf_counter() - start
                    content += delta["content"]
        first_s.append(first or 0.0)
        total_s.append(time.perf_counter() - start)

    think_tag = "<think>" in content
    c.ok = bool(content.strip())
    c.details = {
        "answer_preview": content.strip()[:150],
        "extra_body": extra or "(없음)",
        "reasoning_text_received": bool(reasoning) or think_tag,
        "queue_wait_s_median": round(statistics.median(header_s), 1),
        "first_answer_char_s_median": round(statistics.median(first_s), 1),
        "total_s_median": round(statistics.median(total_s), 1),
        "prd_target": "첫 토큰 3초, 전체 p95 8초 (PRD 7장)",
    }
    c.summary = (
        f"응답 정상, 대기 {c.details['queue_wait_s_median']}초, "
        f"첫 답변 글자 {c.details['first_answer_char_s_median']}초, "
        f"전체 {c.details['total_s_median']}초({runs}회 중앙값), "
        f"추론 텍스트 {'섞임(후처리 필요)' if c.details['reasoning_text_received'] else '없음'}"
    )
    return c


def embed_one(
    client: httpx.Client, env: dict[str, str], text: str, input_type: str, dim: int | None
) -> tuple[httpx.Response, list[float]]:
    """문장 하나를 임베딩한다. 무료 엔드포인트는 여러 문장을 묶은 요청에 500을 돌려준다."""
    body: dict[str, Any] = {
        "model": env["EMBED_MODEL"],
        "input": [text],
        "input_type": input_type,
        "encoding_format": "float",
        "truncate": "END",
        "modality": "text",
    }
    if dim:
        body["dimensions"] = dim
    url = f"{env['NVIDIA_BASE_URL']}/embeddings"
    resp = client.post(url, json=body, headers=auth(env, "embed"))
    vector = resp.json()["data"][0]["embedding"] if resp.status_code == 200 else []
    return resp, vector


def check_embed(client: httpx.Client, env: dict[str, str], runs: int) -> Check:
    c = Check("임베딩")
    say("  임베딩 요청 중")
    resp, native = embed_one(client, env, PASSAGES[0], "passage", None)
    if resp.status_code != 200:
        c.error = explain_http_error(resp)
        return c
    native_dim = len(native)

    target = int(env["EMBED_DIM"])
    times: list[float] = []
    query_vec: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        r, query_vec = embed_one(client, env, QUERY, "query", target)
        times.append(time.perf_counter() - start)
        if r.status_code != 200:
            c.error = f"dimensions={target} 요청 실패 → " + explain_http_error(r)
            return c
    reduced = []
    for p in PASSAGES:
        r, vec = embed_one(client, env, p, "passage", target)
        if r.status_code != 200:
            c.error = "문서 문장 임베딩 실패 → " + explain_http_error(r)
            return c
        reduced.append(vec)
    sims = [round(cosine(query_vec, p), 4) for p in reduced]

    # 참고: 여러 문장을 한 요청에 묶을 수 있는지(TRD 6.2 EMBED_BATCH_SIZE).
    # 실패해도 통과 여부와 무관하다.
    batch = client.post(
        f"{env['NVIDIA_BASE_URL']}/embeddings",
        json={"model": env["EMBED_MODEL"], "input": PASSAGES, "input_type": "passage"},
        headers=auth(env, "embed"),
    )

    best_first = sims.index(max(sims)) == 0
    c.ok = len(query_vec) == target and best_first
    c.details = {
        "native_dim": native_dim,
        "native_dim_fits_halfvec_hnsw": native_dim <= HALFVEC_HNSW_MAX,
        "reduced_dim": len(query_vec),
        "cosine_query_vs_passages": sims,
        "query_embed_s_median": round(statistics.median(times), 2),
        "batch_of_3_http_status": batch.status_code,
    }
    c.summary = (
        f"기본 {native_dim}차원, dimensions={target} 요청 시 {len(query_vec)}차원, "
        f"질문 임베딩 {c.details['query_embed_s_median']}초, "
        f"관련 문장 유사도가 가장 높음: {'예' if best_first else '아니오'}, "
        f"3개 묶음 요청: {'가능' if batch.status_code == 200 else f'불가({batch.status_code})'}"
    )
    return c


def check_rerank(client: httpx.Client, env: dict[str, str], runs: int) -> Check:
    c = Check("재정렬")
    body = {
        "model": env["RERANK_MODEL"],
        "query": {"text": QUERY},
        "passages": [{"text": p} for p in PASSAGES],
        "truncate": "END",
    }
    times: list[float] = []
    resp: httpx.Response | None = None
    for _ in range(runs):
        start = time.perf_counter()
        resp = client.post(env["RERANK_URL"], json=body, headers=auth(env, "rerank"))
        times.append(time.perf_counter() - start)
        if resp.status_code != 200:
            c.error = explain_http_error(resp)
            return c
    assert resp is not None
    rankings = resp.json().get("rankings", [])
    order = [r.get("index") for r in rankings]
    c.ok = bool(order) and order[0] == 0
    c.details = {
        "rankings": rankings,
        "rerank_s_median": round(statistics.median(times), 2),
        "rate_limit_headers": rate_headers(resp),
    }
    c.summary = (
        f"응답 정상, 순서 {order}, {c.details['rerank_s_median']}초, "
        f"관련 문장이 1위: {'예' if c.ok else '아니오'}"
    )
    return c


# --------------------------------------------------------------------------- 진입점


def main() -> int:
    parser = argparse.ArgumentParser(description="NVIDIA API 키·모델 동작 확인")
    parser.add_argument("--only", choices=["llm", "embed", "rerank"])
    parser.add_argument("--runs", type=int, default=3, help="임베딩·재정렬 반복 횟수")
    parser.add_argument("--llm-runs", type=int, default=1, help="LLM 반복 횟수(느려서 기본 1)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    env = load_env()
    kinds = [k for k in KEY_VARS if args.only in (None, k)]
    for kind in kinds:
        var = KEY_VARS[kind]
        key = env.get(var, "")
        label, model_var = LABELS[kind]
        print(f"{label}: {env[model_var]} / {var} = {mask(key) if key else '비어 있음'}")
        if key and not key.startswith("nvapi-"):
            print(f"  경고: {var}가 nvapi-로 시작하지 않습니다. 다른 종류의 키일 수 있습니다.")
    if not any(env.get(KEY_VARS[k]) for k in kinds):
        print("\n키가 하나도 없습니다. 저장소 루트 .env에 키를 넣고 다시 실행하세요.")
        return 2
    print()

    checks: list[Check] = []
    timeout = httpx.Timeout(240, connect=15)  # LLM 대기열 때문에 넉넉히
    with httpx.Client(headers={"Accept": "application/json"}, timeout=timeout) as client:
        steps = {
            "llm": lambda: check_llm(client, env, args.llm_runs),
            "embed": lambda: check_embed(client, env, args.runs),
            "rerank": lambda: check_rerank(client, env, args.runs),
        }
        if not args.only:
            checks.append(safe(lambda: check_models(client, env), "모델 목록"))
        for kind in kinds:
            label = LABELS[kind][0]
            if not env.get(KEY_VARS[kind]):
                checks.append(Check(label, error=f"{KEY_VARS[kind]}가 비어 있어 건너뜀"))
                continue
            say(f"[{label}] 확인 중...")
            checks.append(safe(steps[kind], label))

    for c in checks:
        mark = "통과" if c.ok else "실패"
        print(f"[{mark}] {c.name}: {c.summary or c.error}")
        if c.error and c.summary:
            print(f"        {c.error}")
        for k, v in c.details.items():
            print(f"        {k}: {v}")
    if args.out:
        args.out.write_text(
            json.dumps([c.__dict__ for c in checks], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n결과 저장: {args.out}")
    return 0 if all(c.ok for c in checks) else 1


def safe(step: Any, name: str) -> Check:
    try:
        result: Check = step()
        return result
    except httpx.HTTPError as e:
        return Check(name, error=f"연결 실패: {type(e).__name__}: {e}")
    except (KeyError, ValueError, IndexError) as e:
        return Check(name, error=f"응답 형식이 예상과 다름: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
