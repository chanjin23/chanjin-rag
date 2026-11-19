"""애플리케이션 설정. 값은 환경 변수와 저장소 루트의 `.env`에서 읽는다 (TRD 12.1).

- `.env`는 실행 위치와 상관없이 **저장소 루트의 `.env` 하나**만 읽는다.
  (`backend/`에서 uvicorn을 띄워도 루트 `.env`를 읽는다.)
- 환경 변수가 `.env`보다 우선한다.
- `APP_ENV=test`가 프로세스 환경 변수로 주어지면 `.env`를 읽지 않는다.
  pytest(`backend/tests/conftest.py`)와 E2E(`web/playwright.config.ts`)가 이렇게 실행해,
  실제 API 키가 테스트에 섞이지 않게 한다.
"""

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py → 저장소 루트
REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"

AppEnv = Literal["dev", "prod", "test"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: AppEnv = "dev"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] | None = None  # 없으면 prod=json, 그 밖에는 console
    log_file: str | None = None  # 주면 stdout과 함께 이 파일에도 기록 (E2E에서 사용)
    # 앱 로그에 질문 원문을 남길지(TRD 11장). 운영은 false. 개발·E2E에서만 true로 쓴다.
    log_query_text: bool = False

    @property
    def log_json(self) -> bool:
        if self.log_format is not None:
            return self.log_format == "json"
        return self.app_env == "prod"

    # 모델 Provider. 테스트와 E2E는 항상 fake(결정적인 가짜 구현, 외부 호출 없음)
    provider: Literal["fake", "nvidia"] = "nvidia"

    # 모델 (NVIDIA API Catalog, OpenAI 호환). 모델마다 발급받은 키가 다르다.
    nvidia_llm_api_key: SecretStr | None = None
    nvidia_embed_api_key: SecretStr | None = None
    nvidia_rerank_api_key: SecretStr | None = None
    # 평가 판정(LLM-as-judge) 모델 키(TRD 14장). 판정 모델은 답변 모델과 다른 계열로 고른다
    nvidia_judge_api_key: SecretStr | None = None
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_model: str = "nvidia/nemotron-3.5-lightning-30b-a3b"
    # 모델별 추가 요청값(.env에는 JSON 한 줄). 추론을 끄는 키가 모델마다 달라 값 그대로 보낸다.
    llm_extra_body: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}}
    # PRD 검토값: temperature 0~0.3, max_tokens 1,024~2,048 (TRD 7.1)
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    # 무료 엔드포인트는 대기열 때문에 응답 시작까지 1분 넘게 걸리기도 한다(TRD 13장)
    llm_read_timeout: float = 180.0
    # 임베딩·재정렬 읽기 타임아웃(초)
    provider_read_timeout: float = 60.0
    embed_model: str = "nvidia/nemotron-3.5-embed-8b"
    # 모델 기본 4096차원은 halfvec HNSW 한도(4000)를 넘어서 `dimensions`로 줄인다(TRD 6.3)
    embed_dim: int = 2048
    rerank_model: str = "nvidia/nemotron-3.5-rerank-8b"
    rerank_url: str = (
        "https://ai.api.nvidia.com/v1/retrieval/nvidia/nemotron-3.5-rerank-8b/reranking"
    )
    rerank_enabled: bool = True
    # 평가 판정 모델(PRD 8장: 답변 모델과 달라야 함, `rag-eval run`이 같으면 거절)
    eval_judge_model: str = "meta/llama-3.3-70b-instruct"

    # 질의(TRD 7.1, 7.3). 전체 시간 상한(초)을 넘으면 `timeout` 오류로 끝낸다
    chat_timeout_seconds: float = 60.0
    # Fake LLM 조각 사이 지연(ms, PROVIDER=fake 전용).
    # E2E에서 단계 표시를 화면으로 확인할 수 있게 늦춘다
    fake_llm_token_delay_ms: int = 0

    # 수집
    embed_daily_file_limit: int = 100
    embed_daily_chunk_limit: int = 0  # 0이면 제한 없음(TRD 6.2)
    # 무료 엔드포인트는 묶음 요청에 500을 돌려줘서 1개씩 보낸다(TRD 13장 실측)
    embed_batch_size: int = 1
    # 파서 격리(TRD 3.4, 11장): 하위 프로세스 하나의 시간·메모리 상한. 메모리 0이면 상한 없음
    parser_timeout_seconds: float = 60.0
    parser_memory_limit_mb: int = 1024
    # Kiwi 사용자 사전(TRD 6.4). 없으면 저장소의 deploy/kiwi_user_dict.txt.
    # 파일이 없으면 사전 없이 동작한다
    kiwi_user_dict_path: str | None = None
    # 수집 허용 최상위 폴더(PRD 3장: C:\Chanjin\dev 하위만). 고정 제외 `/project/`, `/backup/`은
    # 이 폴더 기준이다. 테스트·E2E는 임시 폴더로 바꿔 같은 검증을 적용한다.
    sync_allowed_root: str = r"C:\Chanjin\dev"
    # 업로드 원본 임시 보관 폴더(TRD 3.2 `tmp` 볼륨). 추출이 끝나면(실패해도) 지운다.
    # 비우면 저장소의 var/tmp
    upload_tmp_dir: str | None = None
    # 작업 큐(TRD 6.2): 재시도 상한, 멈춘 작업으로 보는 시간, 빈 큐 확인 간격
    job_max_attempts: int = 5
    job_stale_lock_minutes: int = 10
    worker_poll_seconds: float = 5.0
    # 429가 계속될 때 작업을 미루는 시간(분)
    job_rate_limit_delay_minutes: int = 30
    local_deleted_retention_days: int = 30
    mass_delete_guard_ratio: float = 0.2
    sync_run_stale_minutes: int = 10
    # 상태 점검(TRD 8.3 `/health`, 12.4): Worker 하트비트가 이 시간(초) 넘게 없으면 이상,
    # 디스크 여유가 이 크기(GB) 미만이면 경고. 디스크 확인 경로를 비우면 업로드 임시 폴더
    disk_check_path: str | None = None
    worker_stale_seconds: int = 90
    disk_min_free_gb: float = 10.0

    # DB. 테스트·E2E는 TEST_DATABASE_URL(5433의 테스트 DB)을 DATABASE_URL로 넘긴다
    database_url: str = "postgresql+psycopg://rag:rag@localhost:5432/rag"

    # 웹 세션(TRD 11장). 운영은 항상 Secure. 로컬 HTTP(E2E 등)에서만 false로 둔다.
    session_cookie_secure: bool = True
    session_ttl_days: int = 14
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    # CSRF: Origin이 요청 Host와 다를 때 추가로 허용할 출처(쉼표로 구분, 예: https://rag.example.ts.net)
    csrf_trusted_origins: str = ""

    @property
    def upload_tmp_path(self) -> Path:
        return Path(self.upload_tmp_dir) if self.upload_tmp_dir else REPO_ROOT / "var" / "tmp"

    @property
    def disk_check_target(self) -> Path:
        return Path(self.disk_check_path) if self.disk_check_path else self.upload_tmp_path

    @property
    def csrf_trusted_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.csrf_trusted_origins.split(",") if o.strip()]

    # 운영
    tz: str = "Asia/Seoul"
    backup_age_recipient: str | None = None
    backup_keep_on_vm: int = 3
    # 백업 파일 폴더(TRD 12.2 `backups/`). 비우면 저장소의 var/backups
    backup_dir: str | None = None
    # 백업 외부 명령(JSON 목록, 앞부분만. 인자는 코드가 붙인다). E2E는 가짜 명령 스크립트를 넘긴다
    backup_pg_dump_cmd: list[str] = ["pg_dump"]
    backup_age_cmd: list[str] = ["age"]

    @property
    def backup_path(self) -> Path:
        return Path(self.backup_dir) if self.backup_dir else REPO_ROOT / "var" / "backups"

    @model_validator(mode="after")
    def _guard_test_env(self) -> "Settings":
        # 테스트·E2E에서는 실제 모델을 부르지 않는다. 셸 환경 변수에 키가 있어도 버린다.
        if self.app_env == "test":
            if self.provider != "fake":
                raise ValueError("APP_ENV=test에서는 PROVIDER=fake만 쓸 수 있습니다")
            self.nvidia_llm_api_key = None
            self.nvidia_embed_api_key = None
            self.nvidia_rerank_api_key = None
            self.nvidia_judge_api_key = None
        if self.app_env == "prod" and not self.session_cookie_secure:
            raise ValueError("APP_ENV=prod에서는 SESSION_COOKIE_SECURE=false를 쓸 수 없습니다")
        return self


def env_file_for(environ: Mapping[str, str]) -> Path | None:
    """읽을 `.env` 경로. 프로세스 환경 변수가 `APP_ENV=test`면 `.env`를 읽지 않는다."""
    if environ.get("APP_ENV", "").strip().lower() == "test":
        return None
    return ENV_FILE


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=env_file_for(os.environ))  # type: ignore[call-arg]
