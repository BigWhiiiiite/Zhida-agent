"""Narrow PostgreSQL adapter for the existing, allowlisted workbench SQL.

It is not a general SQLite emulator. Unknown SQL fails instead of silently
falling back to the old file. Tenant scope is transaction-local and enforced by
PostgreSQL RLS. The canonical workbench write and V2 projection commit together.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import os
import re
import sqlite3

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .db import Database
from .workbench_schema import WORKBENCH_TABLES


def enabled() -> bool:
    selected = os.getenv("ZHIDA_STORAGE_BACKEND", "sqlite").strip().lower()
    if selected not in {"sqlite", "postgresql"}:
        raise RuntimeError("ZHIDA_STORAGE_BACKEND 必须是 sqlite 或 postgresql；不会静默回退")
    return selected == "postgresql"


@lru_cache(maxsize=4)
def _database(url: str) -> Database:
    return Database(url)


def database() -> Database:
    url = os.getenv("ZHIDA_V2_DATABASE_URL", "")
    if not url:
        raise RuntimeError("PostgreSQL 模式缺少 ZHIDA_V2_DATABASE_URL；不会使用旧 SQLite")
    return _database(url)


def assert_ready() -> None:
    with database().transaction() as cx:
        if not cx.execute(text("SELECT to_regclass('workbench.resumes')")).scalar():
            raise RuntimeError("PostgreSQL 工作台迁移尚未完成，请先运行 Alembic；不会自动创建表")


class Row(dict):
    """sqlite3.Row compatible string/integer access without exposing raw tuples."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self, result=None):
        self.result = result
        self.rowcount = result.rowcount if result is not None else 0

    def fetchone(self):
        if self.result is None:
            return None
        row = self.result.mappings().first()
        return Row(row) if row is not None else None

    def fetchall(self):
        return [Row(row) for row in self.result.mappings().all()] if self.result is not None else []


def bind_qmarks(statement: str, parameters=()) -> tuple[str, dict]:
    """Replace only SQL placeholders, never question marks inside quoted text."""
    result, quoted, position, index = [], None, 0, 0
    while position < len(statement):
        character = statement[position]
        if quoted:
            result.append(character)
            if character == quoted:
                if position + 1 < len(statement) and statement[position + 1] == quoted:
                    result.append(statement[position + 1])
                    position += 1
                else:
                    quoted = None
        elif character in {"'", '"'}:
            quoted = character
            result.append(character)
        elif character == "?":
            result.append(":p" + str(index))
            index += 1
        else:
            result.append(character)
        position += 1
    if quoted or index != len(parameters):
        raise ValueError("工作台 SQL 占位符与参数不一致")
    return "".join(result), {"p" + str(i): value for i, value in enumerate(parameters)}


class Connection:
    def __init__(self, sa_conn, user_id: str):
        self.sa_conn = sa_conn
        self.user_id = user_id
        self.changed_tables: set[str] = set()
        self._locked = False
        sa_conn.execute(text("SET LOCAL search_path TO workbench, public"))

    def set_tenant(self, user_id: str) -> None:
        if self.user_id and self.user_id != user_id:
            raise RuntimeError("不能在同一工作台事务切换用户")
        self.user_id = user_id
        self.sa_conn.execute(text("SELECT set_config('zhida.user_id',:u,true)"), {"u": user_id})

    def _lock(self):
        if not self._locked:
            # Equivalent write-serialization boundary to BEGIN IMMEDIATE, scoped
            # to one owner instead of locking every user's database.
            self.sa_conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                                 {"key": "workbench:" + (self.user_id or "auth")})
            self._locked = True

    def execute(self, statement: str, parameters=()):
        stripped = statement.strip()
        if stripped.upper() == "BEGIN IMMEDIATE":
            self._lock()
            return Cursor()
        if not re.match(r"^(SELECT|INSERT|UPDATE|DELETE)\b", stripped, flags=re.I):
            raise ValueError("PostgreSQL 工作台只接受迁移后已知的读写 SQL，不接受运行时 DDL")
        mutation = re.match(r"^(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)\b", stripped, re.I)
        if mutation:
            table = mutation.group(1).lower()
            if table not in WORKBENCH_TABLES:
                raise ValueError("不能修改工作台允许清单之外的表")
            self._lock()
        # SQLite NOCASE is used only for account lookup; lower() matches the
        # canonical lower(email) unique PostgreSQL index.
        stripped = re.sub(r"\bemail\s*=\s*\?\s+COLLATE\s+NOCASE", "lower(email)=lower(?)", stripped, flags=re.I)
        if "rag_evidence_fts MATCH" in stripped:
            # Compatibility-only lexical path. New evidence retrieval uses the
            # V2 Chinese token GIN + HNSW implementation, never this old cache.
            stripped = """SELECT chunk_id FROM rag_evidence_fts
                WHERE document @@ plainto_tsquery('simple',?) AND user_id=?
                ORDER BY chunk_id LIMIT 100"""
            parameters = (parameters[0].strip('"').replace('""', '"'), parameters[1])
        bound, values = bind_qmarks(stripped, parameters)
        try:
            result = self.sa_conn.execute(text(bound), values)
        except IntegrityError as exc:
            # Existing auth already translates this error to a public 409-ish
            # message. Do not include driver parameters/private values.
            raise sqlite3.IntegrityError("PostgreSQL 工作台数据约束冲突") from exc
        if mutation and result.rowcount:
            self.changed_tables.add(table)
        return Cursor(result)

    def project(self) -> None:
        if self.changed_tables and self.user_id:
            from .workbench_projection import sync_user
            sync_user(self.sa_conn, self.user_id, self.changed_tables)


@contextmanager
def connection(user_id: str | None = None):
    if user_id is None:
        from ..storage import _CURRENT_USER_ID
        user_id = _CURRENT_USER_ID.get()
    with database().transaction(user_id or None) as cx:
        adapter = Connection(cx, user_id or "")
        yield adapter
        adapter.project()
