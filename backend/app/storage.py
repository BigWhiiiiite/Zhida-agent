from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import ResumeProfile, ResumeRecord


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "resumes.db"


def _connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize() -> None:
    with _connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resumes (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                label TEXT NOT NULL,
                profile_json TEXT NOT NULL,
                parser TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def _record(row: sqlite3.Row) -> ResumeRecord:
    return ResumeRecord(
        id=row["id"], filename=row["filename"], label=row["label"],
        profile=ResumeProfile.model_validate_json(row["profile_json"]),
        parser=row["parser"], created_at=row["created_at"], updated_at=row["updated_at"],
    )


def list_resumes() -> list[ResumeRecord]:
    with _connection() as conn:
        rows = conn.execute("SELECT * FROM resumes ORDER BY updated_at DESC").fetchall()
    return [_record(row) for row in rows]


def get_resume(resume_id: str) -> ResumeRecord | None:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
    return _record(row) if row else None


def create_resume(resume_id: str, filename: str, label: str, profile: ResumeProfile, parser: str) -> ResumeRecord:
    now = datetime.now(timezone.utc).isoformat()
    with _connection() as conn:
        conn.execute(
            "INSERT INTO resumes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (resume_id, filename, label, profile.model_dump_json(), parser, now, now),
        )
    return get_resume(resume_id)  # type: ignore[return-value]


def update_resume(resume_id: str, label: str, profile: ResumeProfile) -> ResumeRecord | None:
    now = datetime.now(timezone.utc).isoformat()
    with _connection() as conn:
        result = conn.execute(
            "UPDATE resumes SET label = ?, profile_json = ?, updated_at = ? WHERE id = ?",
            (label, profile.model_dump_json(), now, resume_id),
        )
    return get_resume(resume_id) if result.rowcount else None
