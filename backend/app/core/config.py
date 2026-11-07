"""애플리케이션 설정. 값은 환경 변수와 `.env`에서 읽는다 (TRD 12.1)."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] | None = None  # 없으면 dev=console, prod=json
    log_file: str | None = None  # 주면 stdout과 함께 이 파일에도 기록 (E2E에서 사용)

    @property
    def log_json(self) -> bool:
        if self.log_format is not None:
            return self.log_format == "json"
        return self.app_env == "prod"

    # 모델 (NVIDIA API Catalog, OpenAI 호환)
    nvidia_api_key: SecretStr | None = None
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_model: str = "deepseek-ai/deepseek-v4.1-flash"
    embed_model: str = "nvidia/nemotron-3.5-embed-8b"
    rerank_model: str = "nvidia/nemotron-3.5-rerank-8b"
    rerank_url: str | None = None
    rerank_enabled: bool = True

    # 수집
    embed_daily_file_limit: int = 100
    embed_batch_size: int = 32

    # DB (구조만 잡아 둠, 아직 연결하지 않음)
    database_url: str = "postgresql+psycopg://rag:rag@localhost:5432/rag"


@lru_cache
def get_settings() -> Settings:
    return Settings()
