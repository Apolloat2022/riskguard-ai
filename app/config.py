from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Every value is sourced from the environment —
    no secret or connection string is ever hardcoded."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://user:pass@localhost/riskguard"

    aws_region: str = "us-east-1"
    bedrock_model_id: str = "anthropic.claude-sonnet-5"

    model_artifact_dir: Path = Path("ml/artifacts/v1")

    risk_trigger_threshold: float = 0.70

    checkpointer_backend: Literal["memory", "postgres"] = "memory"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
