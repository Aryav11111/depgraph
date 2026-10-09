"""Application settings, loaded exclusively from environment variables.

Every knob lives here so the rest of the code never reads os.environ directly.
Values can also come from a local `.env` file for development; in Cloud Run they
come from the service's env vars / Secret Manager bindings.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"
    # JSON logs for containers/Cloud Logging; pretty console output when False.
    log_json: bool = True

    # SecretStr keeps the password out of reprs and accidental log lines.
    database_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://depgraph:depgraph@localhost:5432/depgraph")
    )
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False

    redis_url: SecretStr = Field(default=SecretStr("redis://localhost:6379/0"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
