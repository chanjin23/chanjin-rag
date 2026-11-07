"""설정: 저장소 루트 `.env` 하나만 읽고, 테스트 환경에서는 `.env`와 실제 키를 쓰지 않는다."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import ENV_FILE, REPO_ROOT, Settings, env_file_for, get_settings


def test_env_file_is_repo_root_regardless_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (REPO_ROOT / "pyproject.toml").is_file()
    assert (REPO_ROOT / "backend").is_dir()
    assert ENV_FILE == REPO_ROOT / ".env"

    monkeypatch.chdir(tmp_path)
    assert env_file_for({"APP_ENV": "dev"}) == ENV_FILE
    assert env_file_for({}) == ENV_FILE


def test_root_env_file_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("PROVIDER", raising=False)
    env = tmp_path / ".env"
    env.write_text("EMBED_DIM=1024\nLOG_QUERY_TEXT=true\n", encoding="utf-8")
    s = Settings(_env_file=env)
    assert s.embed_dim == 1024
    assert s.log_query_text is True


def test_test_env_ignores_dotenv_and_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    assert env_file_for({"APP_ENV": "test"}) is None
    monkeypatch.setenv("NVIDIA_LLM_API_KEY", "nvapi-should-not-be-used")
    s = get_settings()
    assert s.app_env == "test"
    assert s.provider == "fake"
    assert s.nvidia_llm_api_key is None
    assert s.nvidia_embed_api_key is None
    assert s.nvidia_rerank_api_key is None

    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="test", provider="nvidia")


def test_trd_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("PROVIDER", raising=False)
    s = Settings(_env_file=None)
    assert s.provider == "nvidia"
    assert s.embed_dim == 2048
    assert s.embed_batch_size == 1
    assert s.embed_daily_file_limit == 100
    assert s.embed_daily_chunk_limit == 0
    assert s.local_deleted_retention_days == 30
    assert s.mass_delete_guard_ratio == 0.2
    assert s.sync_run_stale_minutes == 10
    assert s.log_query_text is False
    assert s.tz == "Asia/Seoul"
    assert s.backup_keep_on_vm == 3
