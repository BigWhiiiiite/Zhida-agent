from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import CandidateProfile, FieldEvidence, ProfileConflict, ResumeProfile, ResumeRecord


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "resumes.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def initialize() -> None:
    with _connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resumes (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, label TEXT NOT NULL,
                profile_json TEXT NOT NULL, parser TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        _add_missing_columns(conn, "resumes", {
            "stored_filename": "TEXT NOT NULL DEFAULT ''", "status": "TEXT NOT NULL DEFAULT 'needs_review'",
            "language": "TEXT NOT NULL DEFAULT '未识别'", "tags_json": "TEXT NOT NULL DEFAULT '[]'",
            "target_role": "TEXT NOT NULL DEFAULT ''", "is_default": "INTEGER NOT NULL DEFAULT 0",
            "file_size": "INTEGER NOT NULL DEFAULT 0", "content_hash": "TEXT NOT NULL DEFAULT ''",
            "raw_text": "TEXT NOT NULL DEFAULT ''", "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            "error_message": "TEXT NOT NULL DEFAULT ''",
        })
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidate_profiles (
                id TEXT PRIMARY KEY, profile_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conflicts (
                id TEXT PRIMARY KEY, field_path TEXT NOT NULL,
                current_value_json TEXT NOT NULL, incoming_value_json TEXT NOT NULL,
                resume_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                resolution_json TEXT, created_at TEXT NOT NULL
            )
        """)
        if not conn.execute("SELECT id FROM candidate_profiles WHERE id='default'").fetchone():
            now = _now()
            conn.execute("INSERT INTO candidate_profiles VALUES ('default', ?, ?, ?)", (ResumeProfile().model_dump_json(), now, now))


def _record(row: sqlite3.Row) -> ResumeRecord:
    evidence = [FieldEvidence.model_validate(item) for item in json.loads(row["evidence_json"] or "[]")]
    return ResumeRecord(
        id=row["id"], filename=row["filename"], label=row["label"],
        profile=ResumeProfile.model_validate_json(row["profile_json"]), parser=row["parser"],
        status=row["status"], language=row["language"], tags=json.loads(row["tags_json"] or "[]"),
        target_role=row["target_role"], is_default=bool(row["is_default"]),
        file_size=row["file_size"], content_hash=row["content_hash"], error_message=row["error_message"], evidence=evidence,
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def list_resumes() -> list[ResumeRecord]:
    with _connection() as conn:
        rows = conn.execute("SELECT * FROM resumes ORDER BY is_default DESC, updated_at DESC").fetchall()
    return [_record(row) for row in rows]


def get_resume(resume_id: str) -> ResumeRecord | None:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
    return _record(row) if row else None


def get_resume_internal(resume_id: str) -> sqlite3.Row | None:
    with _connection() as conn:
        return conn.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()


def find_by_hash(content_hash: str) -> ResumeRecord | None:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM resumes WHERE content_hash = ? AND content_hash != ''", (content_hash,)).fetchone()
    return _record(row) if row else None


def create_resume(resume_id: str, filename: str, stored_filename: str, label: str, profile: ResumeProfile,
                  parser: str, language: str, file_size: int, content_hash: str, raw_text: str,
                  evidence: list[FieldEvidence]) -> ResumeRecord:
    now = _now()
    with _connection() as conn:
        conn.execute("""INSERT INTO resumes
            (id, filename, label, profile_json, parser, created_at, updated_at, stored_filename,
             status, language, tags_json, target_role, is_default, file_size, content_hash, raw_text, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'needs_review', ?, '[]', ?, 0, ?, ?, ?, ?)""",
            (resume_id, filename, label, profile.model_dump_json(), parser, now, now, stored_filename,
             language, profile.target_role, file_size, content_hash, raw_text,
             json.dumps([item.model_dump(mode="json") for item in evidence], ensure_ascii=False)))
    return get_resume(resume_id)  # type: ignore[return-value]


def create_pending_resume(resume_id: str, filename: str, stored_filename: str, label: str, parser: str,
                          language: str, file_size: int, content_hash: str, raw_text: str) -> ResumeRecord:
    now = _now()
    with _connection() as conn:
        conn.execute("""INSERT INTO resumes
            (id, filename, label, profile_json, parser, created_at, updated_at, stored_filename,
             status, language, tags_json, target_role, is_default, file_size, content_hash, raw_text,
             evidence_json, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'parsing', ?, '[]', '', 0, ?, ?, ?, '[]', '')""",
            (resume_id, filename, label, ResumeProfile().model_dump_json(), parser, now, now, stored_filename,
             language, file_size, content_hash, raw_text))
    return get_resume(resume_id)  # type: ignore[return-value]


def update_resume(resume_id: str, **changes: Any) -> ResumeRecord | None:
    current = get_resume(resume_id)
    if not current:
        return None
    label = changes.get("label", current.label); profile = changes.get("profile", current.profile)
    language = changes.get("language", current.language); tags = changes.get("tags", current.tags)
    target_role = changes.get("target_role", current.target_role); is_default = changes.get("is_default", current.is_default)
    with _connection() as conn:
        if is_default:
            conn.execute("UPDATE resumes SET is_default = 0")
        conn.execute("""UPDATE resumes SET label=?, profile_json=?, language=?, tags_json=?, target_role=?,
               is_default=?, updated_at=? WHERE id=?""",
            (label, profile.model_dump_json(), language, json.dumps(tags, ensure_ascii=False), target_role,
             int(is_default), _now(), resume_id))
    return get_resume(resume_id)


def replace_parse_result(resume_id: str, profile: ResumeProfile, parser: str, evidence: list[FieldEvidence]) -> ResumeRecord | None:
    with _connection() as conn:
        conn.execute("""UPDATE resumes SET profile_json=?, parser=?, evidence_json=?, target_role=?,
            status='needs_review', error_message='', updated_at=? WHERE id=?""",
            (profile.model_dump_json(), parser, json.dumps([e.model_dump(mode='json') for e in evidence], ensure_ascii=False),
             profile.target_role, _now(), resume_id))
    return get_resume(resume_id)


def mark_resume_parsing(resume_id: str) -> ResumeRecord | None:
    with _connection() as conn:
        conn.execute("UPDATE resumes SET status='parsing', error_message='', updated_at=? WHERE id=?",
                     (_now(), resume_id))
    return get_resume(resume_id)


def mark_resume_failed(resume_id: str, message: str) -> ResumeRecord | None:
    with _connection() as conn:
        conn.execute("UPDATE resumes SET status='failed', error_message=?, updated_at=? WHERE id=?",
                     (message[:500], _now(), resume_id))
    return get_resume(resume_id)


def delete_resume(resume_id: str) -> str | None:
    row = get_resume_internal(resume_id)
    if not row:
        return None
    with _connection() as conn:
        conn.execute("DELETE FROM conflicts WHERE resume_id=?", (resume_id,))
        conn.execute("DELETE FROM resumes WHERE id=?", (resume_id,))
    return row["stored_filename"]


def get_profile() -> CandidateProfile:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM candidate_profiles WHERE id='default'").fetchone()
    if not row:
        initialize(); return get_profile()
    return CandidateProfile(**ResumeProfile.model_validate_json(row["profile_json"]).model_dump(), id="default",
                            created_at=row["created_at"], updated_at=row["updated_at"])


def save_profile(profile: ResumeProfile) -> CandidateProfile:
    with _connection() as conn:
        conn.execute("UPDATE candidate_profiles SET profile_json=?, updated_at=? WHERE id='default'", (profile.model_dump_json(), _now()))
    return get_profile()


def create_conflict(field_path: str, current_value: Any, incoming_value: Any, resume_id: str) -> None:
    incoming_json = json.dumps(incoming_value, ensure_ascii=False)
    with _connection() as conn:
        exists = conn.execute("SELECT id FROM conflicts WHERE field_path=? AND incoming_value_json=? AND resume_id=? AND status='pending'",
                              (field_path, incoming_json, resume_id)).fetchone()
        if not exists:
            conn.execute("INSERT INTO conflicts VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?)",
                (str(uuid4()), field_path, json.dumps(current_value, ensure_ascii=False), incoming_json, resume_id, _now()))


def _conflict(row: sqlite3.Row) -> ProfileConflict:
    resume = get_resume(row["resume_id"])
    return ProfileConflict(id=row["id"], field_path=row["field_path"], current_value=json.loads(row["current_value_json"]),
        incoming_value=json.loads(row["incoming_value_json"]), resume_id=row["resume_id"],
        resume_label=resume.label if resume else "已删除简历", status=row["status"],
        resolution=json.loads(row["resolution_json"]) if row["resolution_json"] else None, created_at=row["created_at"])


def list_conflicts(pending_only: bool = True) -> list[ProfileConflict]:
    query = "SELECT * FROM conflicts" + (" WHERE status='pending'" if pending_only else "") + " ORDER BY created_at DESC"
    with _connection() as conn:
        rows = conn.execute(query).fetchall()
    return [_conflict(row) for row in rows]


def resolve_conflict(conflict_id: str, resolution: Any) -> ProfileConflict | None:
    with _connection() as conn:
        conn.execute("UPDATE conflicts SET status='resolved', resolution_json=? WHERE id=?",
                     (json.dumps(resolution, ensure_ascii=False), conflict_id))
        row = conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
    return _conflict(row) if row else None


def get_conflict(conflict_id: str) -> ProfileConflict | None:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
    return _conflict(row) if row else None


def update_evidence(resume_id: str, evidence_id: str, status: str, value: Any | None) -> ResumeRecord | None:
    row = get_resume_internal(resume_id)
    if not row:
        return None
    evidence = json.loads(row["evidence_json"] or "[]")
    found = False
    for item in evidence:
        if item["id"] == evidence_id:
            item["status"] = status
            if value is not None: item["value"] = value
            found = True; break
    if not found: return None
    resume_status = "completed" if all(item["status"] != "pending_review" for item in evidence) else "needs_review"
    with _connection() as conn:
        conn.execute("UPDATE resumes SET evidence_json=?, status=?, updated_at=? WHERE id=?",
                     (json.dumps(evidence, ensure_ascii=False), resume_status, _now(), resume_id))
    return get_resume(resume_id)


def raw_text_for(resume_id: str) -> str | None:
    row = get_resume_internal(resume_id)
    return row["raw_text"] if row else None
