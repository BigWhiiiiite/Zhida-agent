from __future__ import annotations

import json
import sqlite3
from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import CandidateProfile, FieldEvidence, ProfileConflict, ResumeProfile, ResumeRecord


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "resumes.db"
LOCAL_USER_ID = "local-development-user"
_CURRENT_USER_ID: ContextVar[str] = ContextVar("zhida_current_user_id", default="")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_current_user(user_id: str) -> Token[str]:
    return _CURRENT_USER_ID.set(user_id)


def reset_current_user(token: Token[str]) -> None:
    _CURRENT_USER_ID.reset(token)


def current_user_id() -> str:
    user_id = _CURRENT_USER_ID.get()
    if not user_id:
        raise RuntimeError("当前请求没有已登录用户")
    return user_id


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


def _migrate_application_queue(conn: sqlite3.Connection) -> None:
    indexes = conn.execute("PRAGMA index_list(application_queue)").fetchall()
    for index in indexes:
        if not index["unique"]:
            continue
        columns = [row["name"] for row in conn.execute(f"PRAGMA index_info('{index['name']}')").fetchall()]
        if columns != ["job_id"]:
            continue
        conn.execute("""
            CREATE TABLE application_queue_v2 (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, job_id TEXT NOT NULL,
                resume_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(user_id, job_id)
            )
        """)
        conn.execute("""INSERT INTO application_queue_v2
            (id, user_id, job_id, resume_id, status, created_at, updated_at)
            SELECT id, user_id, job_id, resume_id, status, created_at, updated_at FROM application_queue""")
        conn.execute("DROP TABLE application_queue")
        conn.execute("ALTER TABLE application_queue_v2 RENAME TO application_queue")
        break


def initialize() -> None:
    with _connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL,
                failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL
            )
        """)
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
            "error_message": "TEXT NOT NULL DEFAULT ''", "user_id": "TEXT NOT NULL DEFAULT ''",
        })
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidate_profiles (
                id TEXT PRIMARY KEY, profile_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        _add_missing_columns(conn, "candidate_profiles", {"user_id": "TEXT NOT NULL DEFAULT ''"})
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conflicts (
                id TEXT PRIMARY KEY, field_path TEXT NOT NULL,
                current_value_json TEXT NOT NULL, incoming_value_json TEXT NOT NULL,
                resume_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                resolution_json TEXT, created_at TEXT NOT NULL
            )
        """)
        _add_missing_columns(conn, "conflicts", {"user_id": "TEXT NOT NULL DEFAULT ''"})
        conn.execute("""
            CREATE TABLE IF NOT EXISTS application_queue (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL,
                resume_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned',
                notes TEXT NOT NULL DEFAULT '', application_id TEXT NOT NULL DEFAULT '',
                status_changed_at TEXT NOT NULL DEFAULT '', submitted_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(user_id, job_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_verifications (
                job_id TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at TEXT NOT NULL,
                official_url TEXT NOT NULL, final_url TEXT NOT NULL DEFAULT '',
                http_status INTEGER, page_title TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '[]', message TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discovered_jobs (
                job_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, job_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_source_runs (
                source_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
                jobs_seen INTEGER NOT NULL DEFAULT 0, total_available INTEGER,
                partial INTEGER NOT NULL DEFAULT 1, message TEXT NOT NULL DEFAULT ''
            )
        """)
        _add_missing_columns(conn, "application_queue", {"user_id": "TEXT NOT NULL DEFAULT ''"})
        _migrate_application_queue(conn)
        _add_missing_columns(conn, "application_queue", {
            "notes": "TEXT NOT NULL DEFAULT ''",
            "application_id": "TEXT NOT NULL DEFAULT ''",
            "status_changed_at": "TEXT NOT NULL DEFAULT ''",
            "submitted_at": "TEXT",
        })
        conn.execute("""UPDATE application_queue SET status_changed_at=updated_at
                        WHERE status_changed_at=''""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_resumes_user ON resumes(user_id, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conflicts_user ON conflicts(user_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered_jobs_source ON discovered_jobs(source_id, last_seen_at)")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_user
                        ON candidate_profiles(user_id) WHERE user_id != ''""")


def create_user_account(email: str, display_name: str, password_hash: str) -> sqlite3.Row:
    user_id, now = str(uuid4()), _now()
    with _connection() as conn:
        first_user = not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        conn.execute("""INSERT INTO users
            (id, email, display_name, password_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)""", (user_id, email, display_name, password_hash, now, now))
        claimable_profile = conn.execute(
            """SELECT id FROM candidate_profiles WHERE user_id IN ('', ?)
               ORDER BY CASE WHEN user_id=? THEN 0 ELSE 1 END LIMIT 1""",
            (LOCAL_USER_ID, LOCAL_USER_ID),
        ).fetchone()
        if first_user and claimable_profile:
            conn.execute("DELETE FROM candidate_profiles WHERE user_id IN ('', ?) AND id!=?",
                         (LOCAL_USER_ID, claimable_profile["id"]))
            conn.execute("UPDATE candidate_profiles SET user_id=? WHERE id=?",
                         (user_id, claimable_profile["id"]))
            for table in ("resumes", "conflicts", "application_queue"):
                conn.execute(f"UPDATE {table} SET user_id=? WHERE user_id IN ('', ?)",
                             (user_id, LOCAL_USER_ID))
        else:
            conn.execute("""INSERT INTO candidate_profiles (id, user_id, profile_json, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?)""",
                         (user_id, user_id, ResumeProfile().model_dump_json(), now, now))
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def activate_local_user() -> None:
    """Claim pre-auth data for the local workspace without creating login credentials."""
    now = _now()
    with _connection() as conn:
        local_profile = conn.execute(
            "SELECT id FROM candidate_profiles WHERE user_id=? LIMIT 1", (LOCAL_USER_ID,)
        ).fetchone()
        if not local_profile:
            legacy_profile = conn.execute(
                "SELECT id FROM candidate_profiles WHERE user_id='' LIMIT 1"
            ).fetchone()
            if legacy_profile:
                conn.execute("UPDATE candidate_profiles SET user_id=? WHERE id=?",
                             (LOCAL_USER_ID, legacy_profile["id"]))
            else:
                conn.execute("""INSERT INTO candidate_profiles
                    (id, user_id, profile_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (LOCAL_USER_ID, LOCAL_USER_ID, ResumeProfile().model_dump_json(), now, now))
        for table in ("resumes", "conflicts", "application_queue"):
            conn.execute(f"UPDATE {table} SET user_id=? WHERE user_id=''", (LOCAL_USER_ID,))


def get_user_by_email(email: str) -> sqlite3.Row | None:
    with _connection() as conn:
        return conn.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()


def create_session_record(token_hash: str, user_id: str, days: int) -> None:
    now = datetime.now(timezone.utc)
    with _connection() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now.isoformat(),))
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                     (token_hash, user_id, now.isoformat(), (now + timedelta(days=days)).isoformat()))


def get_user_for_session(token_hash: str) -> sqlite3.Row | None:
    now = _now()
    with _connection() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        return conn.execute("""SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id
                               WHERE sessions.token_hash=? AND sessions.expires_at>?""",
                            (token_hash, now)).fetchone()


def delete_session_record(token_hash: str) -> None:
    with _connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))


def record_login_failure(email: str, maximum: int, lockout_minutes: int) -> None:
    with _connection() as conn:
        row = conn.execute("SELECT id, failed_attempts FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        if not row:
            return
        failures = int(row["failed_attempts"]) + 1
        locked_until = ((datetime.now(timezone.utc) + timedelta(minutes=lockout_minutes)).isoformat()
                        if failures >= maximum else None)
        conn.execute("UPDATE users SET failed_attempts=?, locked_until=?, updated_at=? WHERE id=?",
                     (failures, locked_until, _now(), row["id"]))


def clear_login_failures(user_id: str) -> None:
    with _connection() as conn:
        conn.execute("UPDATE users SET failed_attempts=0, locked_until=NULL, updated_at=? WHERE id=?",
                     (_now(), user_id))


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
    user_id = current_user_id()
    with _connection() as conn:
        rows = conn.execute("""SELECT * FROM resumes WHERE user_id=?
                             ORDER BY is_default DESC, updated_at DESC""", (user_id,)).fetchall()
    return [_record(row) for row in rows]


def get_resume(resume_id: str) -> ResumeRecord | None:
    user_id = current_user_id()
    with _connection() as conn:
        row = conn.execute("SELECT * FROM resumes WHERE id=? AND user_id=?", (resume_id, user_id)).fetchone()
    return _record(row) if row else None


def get_resume_internal(resume_id: str) -> sqlite3.Row | None:
    user_id = current_user_id()
    with _connection() as conn:
        return conn.execute("SELECT * FROM resumes WHERE id=? AND user_id=?", (resume_id, user_id)).fetchone()


def find_by_hash(content_hash: str) -> ResumeRecord | None:
    user_id = current_user_id()
    with _connection() as conn:
        row = conn.execute("""SELECT * FROM resumes WHERE user_id=? AND content_hash=?
                            AND content_hash!=''""", (user_id, content_hash)).fetchone()
    return _record(row) if row else None


def create_resume(resume_id: str, filename: str, stored_filename: str, label: str, profile: ResumeProfile,
                  parser: str, language: str, file_size: int, content_hash: str, raw_text: str,
                  evidence: list[FieldEvidence]) -> ResumeRecord:
    user_id, now = current_user_id(), _now()
    with _connection() as conn:
        conn.execute("""INSERT INTO resumes
            (id, user_id, filename, label, profile_json, parser, created_at, updated_at, stored_filename,
             status, language, tags_json, target_role, is_default, file_size, content_hash, raw_text, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'needs_review', ?, '[]', ?, 0, ?, ?, ?, ?)""",
            (resume_id, user_id, filename, label, profile.model_dump_json(), parser, now, now, stored_filename,
             language, profile.target_role, file_size, content_hash, raw_text,
             json.dumps([item.model_dump(mode="json") for item in evidence], ensure_ascii=False)))
    return get_resume(resume_id)  # type: ignore[return-value]


def create_pending_resume(resume_id: str, filename: str, stored_filename: str, label: str, parser: str,
                          language: str, file_size: int, content_hash: str, raw_text: str) -> ResumeRecord:
    user_id, now = current_user_id(), _now()
    with _connection() as conn:
        conn.execute("""INSERT INTO resumes
            (id, user_id, filename, label, profile_json, parser, created_at, updated_at, stored_filename,
             status, language, tags_json, target_role, is_default, file_size, content_hash, raw_text,
             evidence_json, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsing', ?, '[]', '', 0, ?, ?, ?, '[]', '')""",
            (resume_id, user_id, filename, label, ResumeProfile().model_dump_json(), parser, now, now,
             stored_filename, language, file_size, content_hash, raw_text))
    return get_resume(resume_id)  # type: ignore[return-value]


def update_resume(resume_id: str, **changes: Any) -> ResumeRecord | None:
    user_id = current_user_id()
    current = get_resume(resume_id)
    if not current:
        return None
    label = changes.get("label", current.label); profile = changes.get("profile", current.profile)
    language = changes.get("language", current.language); tags = changes.get("tags", current.tags)
    target_role = changes.get("target_role", current.target_role); is_default = changes.get("is_default", current.is_default)
    with _connection() as conn:
        if is_default:
            conn.execute("UPDATE resumes SET is_default=0 WHERE user_id=?", (user_id,))
        conn.execute("""UPDATE resumes SET label=?, profile_json=?, language=?, tags_json=?, target_role=?,
               is_default=?, updated_at=? WHERE id=? AND user_id=?""",
            (label, profile.model_dump_json(), language, json.dumps(tags, ensure_ascii=False), target_role,
             int(is_default), _now(), resume_id, user_id))
    return get_resume(resume_id)


def replace_parse_result(resume_id: str, profile: ResumeProfile, parser: str,
                         evidence: list[FieldEvidence], raw_text: str) -> ResumeRecord | None:
    user_id = current_user_id()
    with _connection() as conn:
        conn.execute("""UPDATE resumes SET profile_json=?, parser=?, evidence_json=?, target_role=?, raw_text=?,
            status='needs_review', error_message='', updated_at=? WHERE id=? AND user_id=?""",
            (profile.model_dump_json(), parser, json.dumps([e.model_dump(mode='json') for e in evidence], ensure_ascii=False),
             profile.target_role, raw_text, _now(), resume_id, user_id))
    return get_resume(resume_id)


def mark_resume_parsing(resume_id: str) -> ResumeRecord | None:
    user_id = current_user_id()
    with _connection() as conn:
        conn.execute("""UPDATE resumes SET status='parsing', error_message='', updated_at=?
                        WHERE id=? AND user_id=?""", (_now(), resume_id, user_id))
    return get_resume(resume_id)


def mark_resume_failed(resume_id: str, message: str) -> ResumeRecord | None:
    user_id = current_user_id()
    with _connection() as conn:
        conn.execute("""UPDATE resumes SET status='failed', error_message=?, updated_at=?
                        WHERE id=? AND user_id=?""", (message[:500], _now(), resume_id, user_id))
    return get_resume(resume_id)


def delete_resume(resume_id: str) -> str | None:
    user_id = current_user_id()
    row = get_resume_internal(resume_id)
    if not row:
        return None
    with _connection() as conn:
        conn.execute("DELETE FROM conflicts WHERE resume_id=? AND user_id=?", (resume_id, user_id))
        conn.execute("DELETE FROM resumes WHERE id=? AND user_id=?", (resume_id, user_id))
    return row["stored_filename"]


def get_profile() -> CandidateProfile:
    user_id = current_user_id()
    with _connection() as conn:
        row = conn.execute("SELECT * FROM candidate_profiles WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            now = _now()
            conn.execute("""INSERT INTO candidate_profiles (id, user_id, profile_json, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?)""",
                         (user_id, user_id, ResumeProfile().model_dump_json(), now, now))
            row = conn.execute("SELECT * FROM candidate_profiles WHERE user_id=?", (user_id,)).fetchone()
    return CandidateProfile(**ResumeProfile.model_validate_json(row["profile_json"]).model_dump(), id=row["id"],
                            created_at=row["created_at"], updated_at=row["updated_at"])


def save_profile(profile: ResumeProfile) -> CandidateProfile:
    user_id = current_user_id()
    with _connection() as conn:
        conn.execute("UPDATE candidate_profiles SET profile_json=?, updated_at=? WHERE user_id=?",
                     (profile.model_dump_json(), _now(), user_id))
    return get_profile()


def create_conflict(field_path: str, current_value: Any, incoming_value: Any, resume_id: str) -> None:
    user_id = current_user_id()
    incoming_json = json.dumps(incoming_value, ensure_ascii=False)
    with _connection() as conn:
        exists = conn.execute("""SELECT id FROM conflicts WHERE user_id=? AND field_path=?
            AND incoming_value_json=? AND resume_id=? AND status='pending'""",
            (user_id, field_path, incoming_json, resume_id)).fetchone()
        if not exists:
            conn.execute("""INSERT INTO conflicts
                (id, user_id, field_path, current_value_json, incoming_value_json, resume_id,
                 status, resolution_json, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?)""",
                (str(uuid4()), user_id, field_path, json.dumps(current_value, ensure_ascii=False),
                 incoming_json, resume_id, _now()))


def _conflict(row: sqlite3.Row) -> ProfileConflict:
    resume = get_resume(row["resume_id"])
    return ProfileConflict(id=row["id"], field_path=row["field_path"], current_value=json.loads(row["current_value_json"]),
        incoming_value=json.loads(row["incoming_value_json"]), resume_id=row["resume_id"],
        resume_label=resume.label if resume else "已删除简历", status=row["status"],
        resolution=json.loads(row["resolution_json"]) if row["resolution_json"] else None, created_at=row["created_at"])


def list_conflicts(pending_only: bool = True) -> list[ProfileConflict]:
    user_id = current_user_id()
    condition = " AND status='pending'" if pending_only else ""
    with _connection() as conn:
        rows = conn.execute(f"SELECT * FROM conflicts WHERE user_id=?{condition} ORDER BY created_at DESC",
                            (user_id,)).fetchall()
    return [_conflict(row) for row in rows]


def resolve_conflict(conflict_id: str, resolution: Any) -> ProfileConflict | None:
    user_id = current_user_id()
    with _connection() as conn:
        conn.execute("""UPDATE conflicts SET status='resolved', resolution_json=?
                        WHERE id=? AND user_id=?""",
                     (json.dumps(resolution, ensure_ascii=False), conflict_id, user_id))
        row = conn.execute("SELECT * FROM conflicts WHERE id=? AND user_id=?", (conflict_id, user_id)).fetchone()
    return _conflict(row) if row else None


def get_conflict(conflict_id: str) -> ProfileConflict | None:
    user_id = current_user_id()
    with _connection() as conn:
        row = conn.execute("SELECT * FROM conflicts WHERE id=? AND user_id=?", (conflict_id, user_id)).fetchone()
    return _conflict(row) if row else None


def update_evidence(resume_id: str, evidence_id: str, status: str, value: Any | None) -> ResumeRecord | None:
    user_id = current_user_id()
    row = get_resume_internal(resume_id)
    if not row:
        return None
    evidence = json.loads(row["evidence_json"] or "[]")
    found = False
    for item in evidence:
        if item["id"] == evidence_id:
            item["status"] = status
            if value is not None:
                item["value"] = value
            found = True
            break
    if not found:
        return None
    resume_status = "completed" if all(item["status"] != "pending_review" for item in evidence) else "needs_review"
    with _connection() as conn:
        conn.execute("""UPDATE resumes SET evidence_json=?, status=?, updated_at=?
                        WHERE id=? AND user_id=?""",
                     (json.dumps(evidence, ensure_ascii=False), resume_status, _now(), resume_id, user_id))
    return get_resume(resume_id)


def raw_text_for(resume_id: str) -> str | None:
    row = get_resume_internal(resume_id)
    return row["raw_text"] if row else None


def add_job_queue_entries(job_ids: list[str], resume_id: str = "") -> None:
    user_id, now = current_user_id(), _now()
    with _connection() as conn:
        for job_id in job_ids:
            existing = conn.execute("SELECT id FROM application_queue WHERE user_id=? AND job_id=?",
                                    (user_id, job_id)).fetchone()
            if existing:
                conn.execute("""UPDATE application_queue SET resume_id=?, updated_at=?
                                WHERE user_id=? AND job_id=?""", (resume_id, now, user_id, job_id))
            else:
                conn.execute("""INSERT INTO application_queue
                    (id, user_id, job_id, resume_id, status, status_changed_at,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'planned', ?, ?, ?)""",
                    (str(uuid4()), user_id, job_id, resume_id, now, now, now))


def list_job_queue_entries() -> list[dict[str, str]]:
    user_id = current_user_id()
    with _connection() as conn:
        rows = conn.execute("""SELECT * FROM application_queue WHERE user_id=?
                             ORDER BY updated_at DESC""", (user_id,)).fetchall()
    return [dict(row) for row in rows]


def delete_job_queue_entry(queue_id: str) -> bool:
    user_id = current_user_id()
    with _connection() as conn:
        cursor = conn.execute("DELETE FROM application_queue WHERE id=? AND user_id=?", (queue_id, user_id))
    return cursor.rowcount > 0


def update_job_queue_entry(queue_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    user_id, now = current_user_id(), _now()
    allowed = {"status", "notes", "application_id", "submitted_at", "status_changed_at"}
    updates = {key: value for key, value in changes.items() if key in allowed}
    updates["updated_at"] = now
    if not updates:
        return None
    assignments = ", ".join(f"{key}=?" for key in updates)
    with _connection() as conn:
        cursor = conn.execute(
            f"UPDATE application_queue SET {assignments} WHERE id=? AND user_id=?",
            (*updates.values(), queue_id, user_id),
        )
        if not cursor.rowcount:
            return None
        row = conn.execute(
            "SELECT * FROM application_queue WHERE id=? AND user_id=?",
            (queue_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def save_job_verification(record: dict[str, Any]) -> None:
    with _connection() as conn:
        conn.execute("""INSERT INTO job_verifications
            (job_id, status, checked_at, official_url, final_url, http_status,
             page_title, evidence_json, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
              status=excluded.status, checked_at=excluded.checked_at,
              official_url=excluded.official_url, final_url=excluded.final_url,
              http_status=excluded.http_status, page_title=excluded.page_title,
              evidence_json=excluded.evidence_json, message=excluded.message""", (
                record["job_id"], record["status"], record["checked_at"],
                record["official_url"], record.get("final_url", ""), record.get("http_status"),
                record.get("page_title", ""),
                json.dumps(record.get("evidence", []), ensure_ascii=False), record.get("message", ""),
            ))


def get_job_verification(job_id: str) -> dict[str, Any] | None:
    try:
        with _connection() as conn:
            row = conn.execute("SELECT * FROM job_verifications WHERE job_id=?", (job_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    result = dict(row)
    result["evidence"] = json.loads(result.pop("evidence_json") or "[]")
    return result


def save_discovered_jobs(source_id: str, jobs: list[dict[str, Any]], seen_at: str) -> tuple[int, int]:
    created = updated = 0
    with _connection() as conn:
        for job in jobs:
            existing = conn.execute(
                "SELECT 1 FROM discovered_jobs WHERE job_id=?", (job["id"],)
            ).fetchone()
            conn.execute("""INSERT INTO discovered_jobs
                (job_id, source_id, job_json, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                  source_id=excluded.source_id, job_json=excluded.job_json,
                  last_seen_at=excluded.last_seen_at""", (
                    job["id"], source_id, json.dumps(job, ensure_ascii=False), seen_at, seen_at,
                ))
            if existing:
                updated += 1
            else:
                created += 1
    return created, updated


def list_discovered_jobs() -> list[dict[str, Any]]:
    try:
        with _connection() as conn:
            rows = conn.execute(
                "SELECT job_json FROM discovered_jobs ORDER BY last_seen_at DESC"
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            result.append(json.loads(row["job_json"]))
        except (TypeError, json.JSONDecodeError):
            continue
    return result


def save_job_source_run(record: dict[str, Any]) -> None:
    with _connection() as conn:
        conn.execute("""INSERT INTO job_source_runs
            (source_id, status, started_at, completed_at, jobs_seen,
             total_available, partial, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
              status=excluded.status, started_at=excluded.started_at,
              completed_at=excluded.completed_at, jobs_seen=excluded.jobs_seen,
              total_available=excluded.total_available, partial=excluded.partial,
              message=excluded.message""", (
                record["source_id"], record["status"], record["started_at"],
                record["completed_at"], record.get("jobs_seen", 0),
                record.get("total_available"), int(bool(record.get("partial", True))),
                record.get("message", ""),
            ))


def get_job_source_run(source_id: str) -> dict[str, Any] | None:
    try:
        with _connection() as conn:
            row = conn.execute(
                "SELECT * FROM job_source_runs WHERE source_id=?", (source_id,)
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    result = dict(row)
    result["partial"] = bool(result["partial"])
    return result
