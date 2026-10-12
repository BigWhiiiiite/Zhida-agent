"""Synthetic-only compatibility migration and PostgreSQL workbench tests."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text

from app import storage
from app.backend_v2.db import Database
from app.backend_v2.legacy_import import backup_sqlite
from app.backend_v2.workbench_import import attachment_manifest, import_snapshot, inspect_snapshot, require_complete_manifest
from app.backend_v2.workbench_storage import Connection, Row, bind_qmarks
from app.models import ResumeProfile


def make_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIDA_STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "original.sqlite3")
    storage.initialize()
    profile = ResumeProfile(name="合成测试用户", education=[{"school": "合成学院", "degree": "硕士"}],
        projects=[{"name": "合成Agent", "description": "工具调用和混合检索"}], target_cities=["北京"],
        application_answers={"是否接受异地": "否"}, application_answer_memory=[{
            "id": "memory-one", "question": "是否接受异地", "normalized_question": "是否接受异地",
            "semantic_key": "preference.relocation", "entity_scope": "application", "value": "否",
            "source_host": "synthetic.invalid", "resume_id": "original-resume", "company_scope": "synthetic.invalid",
            "field_type": "radio", "option_fingerprint": "synthetic-options",
            "updated_at": "2026-10-11T01:00:00+00:00"}])
    owner = "workbench-synthetic-one"
    token = storage.set_current_user(owner)
    try:
        storage.get_profile()
        storage.save_profile(profile)
        storage.create_resume("original-resume", "fixture.txt", "original.txt", "合成Agent简历", profile,
                              "synthetic", "中文", 7, hashlib.sha256(b"fixture").hexdigest(), "fixture", [])
        storage.add_job_queue_entries(["synthetic-job"], "original-resume")
        storage.save_application_agent_checkpoint("synthetic-session", "stopped", "form", "https://synthetic.invalid/form",
                                                 "合成申请页", "observe", "合成测试记录")
    finally:
        storage.reset_current_user(token)
    with sqlite3.connect(storage.DB_PATH) as cx:
        cx.execute("""CREATE TABLE chat_conversations(id TEXT PRIMARY KEY,user_id TEXT,title TEXT,
            conversation_json TEXT,created_at TEXT,updated_at TEXT)""")
        cx.execute("INSERT INTO chat_conversations VALUES(?,?,?,?,?,?)", ("chat-original", owner, "合成会话",
            '{"messages":[{"content":"仅限合成测试"}]}', "2026-10-11", "2026-10-11"))
        cx.execute("""CREATE TABLE application_knowledge(id TEXT PRIMARY KEY,user_id TEXT,site_scope TEXT,
            record_json TEXT,embedding_text TEXT,vector_model TEXT,vector_json TEXT,updated_at TEXT)""")
        cx.execute("INSERT INTO application_knowledge VALUES(?,?,?,?,?,?,?,?)", ("rule-original", owner,
            "synthetic.invalid", '{"question":"学院名称","entity_scope":"education:master"}', "学院名称",
            "old-cache", "[1,2,3]", "2026-10-11"))
        cx.execute("INSERT INTO confirmed_resume_facts VALUES(?,?,?,?,?,?,?,?)", ("confirmed-one", owner,
            "original-resume", "education", "masters-record-key", "college", '{"explicit":true}', "2026-10-11"))
        cx.execute("INSERT INTO sessions VALUES(?,?,?,?)", ("old-token-never-copy", owner, "2026-10-11", "2099-01-01"))
    frozen = tmp_path / "frozen.sqlite3"
    backup_sqlite(storage.DB_PATH, frozen)
    return frozen, profile


def test_qmark_binding_does_not_change_literal_question_marks():
    statement, args = bind_qmarks("SELECT '?' AS literal, 'it''s ?' AS quoted FROM resumes WHERE id=? AND user_id=?", ["r", "u"])
    assert "'?'" in statement and "'it''s ?'" in statement and "id=:p0" in statement
    assert args == {"p0": "r", "p1": "u"}
    with pytest.raises(ValueError):
        bind_qmarks("SELECT ?", [])
    row = Row({"id": "r", "user_id": "u"})
    assert row[0] == row["id"] == dict(row)["id"]


def test_snapshot_preview_preserves_memories_and_reports_ignored_sessions(tmp_path, monkeypatch):
    frozen, profile = make_snapshot(tmp_path, monkeypatch)
    report, records = inspect_snapshot(frozen)
    assert not report["applied"] and not report["unknown_tables"] and not report["unknown_columns"]
    copied = json.loads(records["candidate_profiles"][0]["profile_json"])
    assert copied == profile.model_dump(mode="json")
    assert records["resumes"][0]["id"] == "original-resume"
    assert records["application_knowledge"][0]["vector_json"] == "[]"
    assert report["counts"]["chat_conversations"] == 1
    assert report["counts"]["confirmed_resume_facts"] == 1
    assert report["source_counts"]["sessions"] == 1 and "sessions" not in records
    serialized = json.dumps(report)
    assert "合成测试用户" not in serialized and "old-token-never-copy" not in serialized


def test_unknown_schema_blocks_apply_and_leaves_source_untouched(tmp_path, monkeypatch):
    frozen, _ = make_snapshot(tmp_path, monkeypatch)
    with sqlite3.connect(frozen) as cx:
        cx.execute("CREATE TABLE unknown_business(id TEXT)")
    before = hashlib.sha256(frozen.read_bytes()).hexdigest()
    assert import_snapshot(None, frozen)["unknown_tables"] == ["unknown_business"]
    with pytest.raises(ValueError, match="未支持"):
        import_snapshot(None, frozen, apply=True, frozen=True)
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == before


def test_attachment_manifest_missing_and_changed_file_are_explicit(tmp_path, monkeypatch):
    frozen, _ = make_snapshot(tmp_path, monkeypatch)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    missing = attachment_manifest(frozen, uploads)
    assert missing["missing"] == 1 and missing["verified"] == 0
    with pytest.raises(ValueError, match="不能切换后端"):
        require_complete_manifest(missing)
    (uploads / "original.txt").write_bytes(b"fixture")
    valid = attachment_manifest(frozen, uploads)
    assert valid["verified"] == 1
    require_complete_manifest(valid)
    (uploads / "original.txt").write_bytes(b"modified")
    assert attachment_manifest(frozen, uploads)["mismatch"] == 1


def test_postgres_selection_never_falls_back_to_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIDA_STORAGE_BACKEND", "postgresql")
    monkeypatch.delenv("ZHIDA_V2_DATABASE_URL", raising=False)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "must-not-create.sqlite3")
    with pytest.raises(RuntimeError, match="不会使用旧 SQLite"):
        with storage._connection():
            pass
    assert not storage.DB_PATH.exists()
    monkeypatch.setenv("ZHIDA_STORAGE_BACKEND", "typo")
    with pytest.raises(RuntimeError, match="不会静默回退"):
        storage._connection()


@pytest.fixture
def empty_workbench_db(v2_db):
    if os.getenv("ZHIDA_TEST_ALLOW_CREATE_DATABASE") != "1":
        pytest.skip("Explicit opt-in is required for a synthetic migration database")
    name = "zhida_test_workbench_" + uuid.uuid4().hex
    with v2_db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
        cx.exec_driver_sql('CREATE DATABASE "' + name + '"')
    url = v2_db.engine.url.set(database=name).render_as_string(hide_password=False)
    from app.backend_v2.settings import settings
    previous = os.environ.get("ZHIDA_V2_DATABASE_URL")
    os.environ["ZHIDA_V2_DATABASE_URL"] = url
    settings.cache_clear()
    try:
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic-v2.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("ZHIDA_V2_DATABASE_URL", None)
        else:
            os.environ["ZHIDA_V2_DATABASE_URL"] = previous
        settings.cache_clear()
    db = Database(url)
    try:
        yield db
    finally:
        from app.backend_v2.workbench_storage import _database
        _database.cache_clear()
        db.close()
        with v2_db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
            cx.exec_driver_sql('DROP DATABASE "' + name + '"')


def test_real_import_roundtrip_preserves_all_workbench_records(empty_workbench_db, tmp_path, monkeypatch):
    frozen, profile = make_snapshot(tmp_path, monkeypatch)
    report = import_snapshot(empty_workbench_db, frozen, apply=True, frozen=True)
    assert report["applied"] and report["row_hashes"] == report["verified_row_hashes"]
    assert import_snapshot(empty_workbench_db, frozen, apply=True, frozen=True)["already_imported"]
    monkeypatch.setenv("ZHIDA_STORAGE_BACKEND", "postgresql")
    from app.backend_v2 import workbench_storage
    monkeypatch.setattr(workbench_storage, "database", lambda: empty_workbench_db)
    token = storage.set_current_user("workbench-synthetic-one")
    try:
        assert storage.get_profile().application_answer_memory == profile.application_answer_memory
        assert storage.get_profile().application_answers == profile.application_answers
        assert storage.get_profile().projects == profile.projects
        assert storage.get_resume("original-resume").profile == profile
        assert storage.list_job_queue_entries()[0]["resume_id"] == "original-resume"
        assert storage.confirmed_resume_fact_history("original-resume")[0]["explicit"]
        assert storage.get_application_agent_checkpoint("synthetic-session")["events"][0]["summary"] == "合成测试记录"
        with storage._connection() as cx:
            assert cx.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
            assert cx.execute("SELECT record_json FROM application_knowledge").fetchone()[0] == '{"question":"学院名称","entity_scope":"education:master"}'
            assert cx.execute("SELECT title FROM chat_conversations").fetchone()[0] == "合成会话"
        updated = profile.model_copy(update={"target_cities": ["北京", "上海"]})
        storage.save_profile(updated)
        assert storage.get_profile().target_cities == ["北京", "上海"]
        storage.update_resume("original-resume", label="更新后标签")
        assert storage.get_resume("original-resume").label == "更新后标签"
    finally:
        storage.reset_current_user(token)
    with empty_workbench_db.transaction("workbench-synthetic-one") as cx:
        assert cx.execute(text("SELECT count(*) FROM workbench.projections")).scalar() == 2
        assert cx.execute(text("SELECT count(*) FROM zhida.resume_revisions")).scalar() == 2


def test_real_adapter_auth_create_and_lowercase_lookup(v2_db, monkeypatch):
    monkeypatch.setenv("ZHIDA_STORAGE_BACKEND", "postgresql")
    from app.backend_v2 import workbench_storage
    monkeypatch.setattr(workbench_storage, "database", lambda: v2_db)
    email = uuid.uuid4().hex + "@synthetic.invalid"
    user = storage.create_user_account(email, "合成账号", "disabled")
    assert storage.get_user_by_email(email.upper())["id"] == user["id"]
    storage.create_session_record("synthetic-token-" + user["id"], user["id"], 1)
    assert storage.get_user_for_session("synthetic-token-" + user["id"])["id"] == user["id"]
    token = storage.set_current_user(user["id"])
    try:
        assert storage.get_profile().id == user["id"]
        with storage._connection() as cx:
            cx.execute("BEGIN IMMEDIATE")
            with pytest.raises(RuntimeError):
                cx.set_tenant("another-user")
    finally:
        storage.reset_current_user(token)


def test_private_workbench_rows_are_hidden_from_other_and_unbound_tenants(v2_db):
    role = "zhida_wb_test_" + uuid.uuid4().hex
    owners = ["wb-owner-" + uuid.uuid4().hex, "wb-owner-" + uuid.uuid4().hex]
    with v2_db.transaction() as cx:
        permitted = cx.execute(text("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname=current_user")).scalar()
        if not permitted:
            pytest.skip("This RLS test needs permission to create a temporary non-bypass role")
        cx.execute(text(f'CREATE ROLE "{role}" NOSUPERUSER NOBYPASSRLS NOLOGIN'))
        cx.execute(text(f'GRANT USAGE ON SCHEMA workbench TO "{role}"'))
        cx.execute(text(f'GRANT SELECT ON workbench.candidate_profiles TO "{role}"'))
    try:
        for owner in owners:
            with v2_db.transaction(owner) as cx:
                cx.execute(text("INSERT INTO zhida.users(id) VALUES(:u)"), {"u": owner})
                cx.execute(text("""INSERT INTO workbench.candidate_profiles
                    (id,user_id,profile_json,created_at,updated_at) VALUES(:u,:u,'{}','2026-10-11','2026-10-11')"""), {"u": owner})
        for owner in owners:
            with v2_db.transaction(owner) as cx:
                cx.execute(text(f'SET LOCAL ROLE "{role}"'))
                connection = Connection(cx, owner)
                rows = connection.execute("SELECT user_id FROM candidate_profiles").fetchall()
                assert rows == [{"user_id": owner}]
        with v2_db.transaction() as cx:
            cx.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert cx.execute(text("SELECT count(*) FROM workbench.candidate_profiles")).scalar() == 0
    finally:
        with v2_db.transaction() as cx:
            cx.execute(text(f'DROP OWNED BY "{role}"'))
            cx.execute(text(f'DROP ROLE "{role}"'))


def test_projection_failure_rolls_back_workbench_write(v2_db, monkeypatch):
    monkeypatch.setenv("ZHIDA_STORAGE_BACKEND", "postgresql")
    from app.backend_v2 import workbench_storage, workbench_projection
    monkeypatch.setattr(workbench_storage, "database", lambda: v2_db)
    account = storage.create_user_account(uuid.uuid4().hex + "@synthetic.invalid", "合成回滚", "disabled")
    token = storage.set_current_user(account["id"])
    try:
        original = storage.get_profile()
        def broken_projection(*args):
            raise RuntimeError("synthetic projection failure")
        monkeypatch.setattr(workbench_projection, "sync_user", broken_projection)
        with pytest.raises(RuntimeError, match="synthetic projection failure"):
            storage.save_profile(original.model_copy(update={"name": "must-not-commit"}))
        assert storage.get_profile().name == original.name
    finally:
        storage.reset_current_user(token)
