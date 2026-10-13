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

    # Browser origins allowed to call the API (the web frontend). A JSON list
    # in the env var, e.g. CORS_ORIGINS='["https://depgraph.vercel.app"]'
    cors_origins: list[str] = ["http://localhost:3000"]

    # API limits.
    max_lockfile_bytes: int = 25 * 1024 * 1024
    paths_default_max: int = 100
    paths_hard_max: int = 1000
    paths_max_depth: int = 64
    # Bounds work per request: dependency-path counts grow exponentially.
    paths_max_expansions: int = 200_000

    # OSV advisory ingestion.
    osv_base_url: str = "https://osv-vulnerabilities.storage.googleapis.com"
    osv_ecosystem: str = "npm"
    # Include OpenSSF malicious-package reports (MAL-*, ~97% of npm records).
    osv_include_malicious: bool = True
    osv_batch_size: int = 1000
    osv_download_timeout_s: float = 600.0
    # Incremental sync.
    osv_sync_interval_s: float = 3600.0
    # Re-check records modified up to this long before the cursor (late arrivals).
    osv_sync_overlap_s: int = 3600
    # More changed records than this -> one full download instead.
    osv_incremental_max: int = 5000
    osv_fetch_concurrency: int = 16
    osv_fetch_retries: int = 3

    # npm registry client. Limits are per process (see NOTES.md, step 4).
    npm_registry_url: str = "https://registry.npmjs.org"
    npm_timeout_s: float = 10.0
    npm_max_concurrency: int = 16
    npm_requests_per_second: float = 50.0
    npm_max_retries: int = 4  # attempts = retries + 1
    npm_backoff_base_s: float = 0.5
    npm_backoff_max_s: float = 30.0
    # Cached metadata is served without a request for `fresh` seconds, then
    # revalidated with its ETag; entries are kept `retain` seconds for that.
    npm_cache_fresh_s: int = 3600
    npm_cache_retain_s: int = 7 * 24 * 3600
    npm_negative_cache_s: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
