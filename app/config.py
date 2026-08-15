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
    # app/agent/llm.py uses AnthropicBedrock (the bedrock-runtime InvokeModel
    # path), which wants a region-prefixed cross-region inference profile ID —
    # not a bare "anthropic.<model>" ID. Keep this in sync with .env.example
    # and task-def.json; a mismatch here only surfaces at the first Bedrock
    # call, long after startup.
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    model_artifact_dir: Path = Path("ml/artifacts/v1")

    risk_trigger_threshold: float = 0.70

    checkpointer_backend: Literal["memory", "postgres"] = "memory"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
