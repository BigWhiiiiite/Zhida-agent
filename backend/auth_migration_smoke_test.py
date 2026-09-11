"""Verify legacy single-user data is claimed once and remains isolated."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import SecretStr

from app import storage
from app.auth_models import RegisterRequest
from app.auth_service import register
from app.models import ResumeProfile


original_path = storage.DB_PATH
try:
    with TemporaryDirectory() as temporary:
        storage.DB_PATH = Path(temporary) / "legacy.db"
        now = "2026-09-11T00:00:00+00:00"
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("""CREATE TABLE candidate_profiles (
                id TEXT PRIMARY KEY, profile_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("INSERT INTO candidate_profiles VALUES ('default', ?, ?, ?)",
                         (ResumeProfile(name="Legacy Candidate").model_dump_json(), now, now))
            conn.execute("""CREATE TABLE resumes (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, label TEXT NOT NULL,
                profile_json TEXT NOT NULL, parser TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("INSERT INTO resumes VALUES ('legacy-resume', 'resume.txt', 'Legacy', ?, 'rules', ?, ?)",
                         (ResumeProfile(name="Legacy Candidate").model_dump_json(), now, now))
            conn.execute("""CREATE TABLE conflicts (
                id TEXT PRIMARY KEY, field_path TEXT NOT NULL,
                current_value_json TEXT NOT NULL, incoming_value_json TEXT NOT NULL,
                resume_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                resolution_json TEXT, created_at TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE application_queue (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
                resume_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("INSERT INTO application_queue VALUES ('legacy-queue', 'shared-job', '', 'planned', ?, ?)",
                         (now, now))

        storage.initialize()
        storage.activate_local_user()
        local_context = storage.set_current_user(storage.LOCAL_USER_ID)
        try:
            assert storage.get_profile().name == "Legacy Candidate"
            assert [item.id for item in storage.list_resumes()] == ["legacy-resume"]
        finally:
            storage.reset_current_user(local_context)

        first, _ = register(RegisterRequest(email="first@example.com", password=SecretStr("First-pass-2026")))
        first_context = storage.set_current_user(first.id)
        try:
            assert storage.get_profile().name == "Legacy Candidate"
            assert [item.id for item in storage.list_resumes()] == ["legacy-resume"]
            assert [item["job_id"] for item in storage.list_job_queue_entries()] == ["shared-job"]
        finally:
            storage.reset_current_user(first_context)

        second, _ = register(RegisterRequest(email="second@example.com", password=SecretStr("Second-pass-2026")))
        second_context = storage.set_current_user(second.id)
        try:
            assert storage.get_profile().name == ""
            assert storage.list_resumes() == []
            storage.add_job_queue_entries(["shared-job"])
            assert [item["job_id"] for item in storage.list_job_queue_entries()] == ["shared-job"]
        finally:
            storage.reset_current_user(second_context)
finally:
    storage.DB_PATH = original_path

print("Authentication migration smoke test passed")
