"""Bounded PostgreSQL connections and transaction-scoped tenant identity."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Connection, create_engine, text


class Database:
    def __init__(self, url: str, *, pool_size: int = 3, pool_timeout: int = 5,
                 statement_timeout_ms: int = 15000):
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("V2 requires PostgreSQL; SQLite is not a vector/transaction test substitute")
        self.engine = create_engine(url, pool_size=pool_size, max_overflow=0,
                                    pool_timeout=pool_timeout, pool_pre_ping=True,
                                    hide_parameters=True, connect_args={"connect_timeout": 5})
        self.statement_timeout_ms = statement_timeout_ms

    @contextmanager
    def transaction(self, user_id: str | None = None) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            connection.execute(text("SELECT set_config('zhida.user_id', :user_id, true)"),
                               {"user_id": user_id or ""})
            connection.execute(text("SELECT set_config('statement_timeout', :timeout, true)"),
                               {"timeout": str(self.statement_timeout_ms)})
            yield connection

    def close(self) -> None:
        self.engine.dispose()
