"""Application settings, loaded exclusively from environment variables.

Every knob lives here so the rest of the code never reads os.environ directly.
Values can also come from a local `.env` file for development; in Cloud Run they
come from the service's env vars / Secret Manager bindings.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


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
    # Optional: overrides the password in DATABASE_URL. In production the URL
    # (host, user, db) is plain config and only the password comes from Secret
    # Manager, so the secret is one value shared by Cloud Run and the database VM.
    database_password: SecretStr | None = None
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False

    # Optional cache for npm registry metadata. Empty (REDIS_URL=) disables it:
    # the production demo runs without Redis and fetches from the registry.
    redis_url: SecretStr | None = Field(default=SecretStr("redis://localhost:6379/0"))

    # Browser origins allowed to call the API (the web frontend). A JSON list
    # in the env var, e.g. CORS_ORIGINS='["https://depgraph.vercel.app"]'
    cors_origins: list[str] = ["http://localhost:3000"]
    # Plus origins matching this regex (full match), e.g. Vercel preview deploys:
    # ^https://depgraph-[a-z0-9-]+-<team>\.vercel\.app$  Keep it anchored to your
    # own team slug: a bare *.vercel.app would admit anyone's deployment.
    cors_origin_regex: str | None = None

    # API limits.
    max_lockfile_bytes: int = 25 * 1024 * 1024
    # Upload rate limits, counted in Postgres (fixed windows); 0 disables one.
    upload_limit_per_ip: int = 10
    upload_limit_ip_window_s: int = 600
    upload_limit_global: int = 300
    upload_limit_global_window_s: int = 3600
    # Where the client IP comes from. 0: the TCP peer (no proxy in front).
    # N: the Nth X-Forwarded-For entry counting from the right, i.e. the one
    # written by the Nth trusted proxy. Entries further left are client-supplied
    # and forgeable. Cloud Run: 1 (verified at deploy, see NOTES.md).
    client_ip_proxy_hops: int = 0
    # Scans older than this are deleted by the sync job; 0 keeps them forever.
    scan_retention_days: int = 0
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

    @field_validator("redis_url", "database_password", "cors_origin_regex", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        return None if v == "" else v

    def sqlalchemy_url(self) -> str:
        """DATABASE_URL with DATABASE_PASSWORD (if set) substituted in."""
        url = make_url(self.database_url.get_secret_value())
        if self.database_password is not None:
            url = url.set(password=self.database_password.get_secret_value())
        return url.render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
