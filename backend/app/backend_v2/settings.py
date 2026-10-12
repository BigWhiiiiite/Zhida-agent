"""Explicit environment-only configuration; never loads the real backend .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    broker_url: str = field(default="", repr=False)
    redis_cache_url: str = field(default="", repr=False)
    redis_control_url: str = field(default="", repr=False)
    environment: str = "development"
    local_user_enabled: bool = False
    local_user_id: str = "local-development-user"
    pool_size: int = 3
    pool_timeout: int = 5
    statement_timeout_ms: int = 15000
    cookie_secure: bool = True
    workflow_enabled: bool = False
    recognition_enabled: bool = False
    assisted_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        prefix = "ZHIDA_V2_"
        url = os.getenv(prefix + "DATABASE_URL", "")
        if not url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("配置 ZHIDA_V2_DATABASE_URL 为专用 PostgreSQL 数据库；不会回退到用户 SQLite")
        return cls(
            database_url=url, broker_url=os.getenv(prefix + "BROKER_URL", ""),
            redis_cache_url=os.getenv(prefix + "REDIS_CACHE_URL", ""),
            redis_control_url=os.getenv(prefix + "REDIS_CONTROL_URL", ""),
            environment=os.getenv(prefix + "ENVIRONMENT", "development"),
            local_user_enabled=os.getenv(prefix + "LOCAL_USER_ENABLED", "false").lower() == "true",
            local_user_id=os.getenv(prefix + "LOCAL_USER_ID", "local-development-user"),
            pool_size=int(os.getenv(prefix + "POOL_SIZE", "3")),
            cookie_secure=os.getenv(prefix + "COOKIE_SECURE", "true").lower() == "true",
            workflow_enabled=os.getenv(prefix + "WORKFLOW_ENABLED", "false").lower() == "true",
            recognition_enabled=os.getenv(prefix + "RECOGNITION_ENABLED", "false").lower() == "true",
            assisted_enabled=os.getenv(prefix + "ASSISTED_ENABLED", "false").lower() == "true",
        )


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_env()
