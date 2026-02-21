"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration sourced from environment variables and .env."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    DATABASE_URL: str
    SECRET_KEY: SecretStr
    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8000, ge=1, le=65535)
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "text"] = "text"
    LOG_FILE: Path | None = None
    LOG_FILE_MAX_BYTES: int = Field(default=10_485_760, ge=1)
    LOG_FILE_BACKUP_COUNT: int = Field(default=5, ge=1)
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_JOBSTORE_URL: str = "sqlite:///./scheduler-jobs.sqlite"
    SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS: int = Field(default=300, ge=1)
    SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS: int = Field(default=900, ge=1)
    SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS: int = Field(default=3600, ge=1)
    SCHEDULER_URL_SUBMISSION_BATCH_SIZE: int = Field(default=100, ge=1)
    SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE: int = Field(default=100, ge=1)
    JOB_RECOVERY_AUTO_RESUME: bool = False
    SHUTDOWN_GRACE_PERIOD_SECONDS: int = Field(default=30, ge=1)
    INDEXING_DAILY_QUOTA_LIMIT: int = Field(default=200, ge=0)
    INSPECTION_DAILY_QUOTA_LIMIT: int = Field(default=2000, ge=0)
    OUTBOUND_HTTP_USER_AGENT: str = "BlueBeastBuildAgent"

    @field_validator("LOG_FILE", mode="before")
    @classmethod
    def parse_log_file(cls, value: object) -> object:
        if value in (None, ""):
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()  # type: ignore[call-arg]
