"""Application configuration sourced from environment variables.

A single ``Settings`` instance is shared by the API process and the Celery
workers, so they always agree on database URLs, broker location and which
connectors are enabled.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "workspace-aggregator"
    environment: str = "local"
    log_level: str = "INFO"
    log_json: bool = True

    # Postgres
    postgres_user: str = "aggregator"
    postgres_password: str = "aggregator"
    postgres_db: str = "aggregator"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # Redis (Celery broker + result backend)
    redis_host: str = "redis"
    redis_port: int = 6379

    # S3 / MinIO
    s3_endpoint_url: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "attachments"
    s3_region: str = "us-east-1"

    # Connectors
    github_token: str | None = None
    github_repos: str = "pydantic/pydantic"
    telegram_bot_token: str | None = None
    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: str | None = None
    imap_use_ssl: bool = True
    imap_mailbox: str = "INBOX"
    imap_max_messages: int = 50

    # Optional vector module
    vector_enabled: bool = False
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "messages"
    # Embedding provider: "hash" (deterministic, no deps/keys) or "openai".
    embedding_provider: str = "hash"
    embedding_dim: int = 256  # used by the hash provider / Qdrant collection size
    openai_api_key: str | None = None
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dim: int = 1536

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def github_repo_list(self) -> list[str]:
        return [r.strip() for r in self.github_repos.split(",") if r.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
