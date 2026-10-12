import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.backend_v2.db import Database
from app.backend_v2.legacy_import import backup_sqlite, import_snapshot
from app.backend_v2.repository import Conflict, Repository


def snapshot(tmp_path):
    source = tmp_path / "synthetic-source.sqlite3"
    with sqlite3.connect(source) as cx:
        cx.executescript("""
          CREATE TABLE users(id text,email text,display_name text,password_hash text);
          CREATE TABLE resumes(id text,user_id text,filename text,label text,profile_json text);
          CREATE TABLE candidate_profiles(id text,user_id text,profile_json text);
          CREATE TABLE application_queue(id text,user_id text,job_id text,status text);
          CREATE TABLE sessions(token_hash text);
          CREATE TABLE unsupported_feature(id text);
        """)
        cx.execute("INSERT INTO users VALUES('legacy-one','one@synthetic.invalid','合成用户','disabled')")
        profile = {"name": "合成用户", "education": [{"school": "合成学校", "degree": "硕士"}],
                   "projects": [{"name": "合成项目", "description": "Agent工具调用"}]}
        cx.execute("INSERT INTO resumes VALUES('r1','legacy-one','fixture.pdf','Agent版',?)", (json.dumps(profile),))
        cx.execute("INSERT INTO candidate_profiles VALUES('p1','legacy-one',?)", (json.dumps({"name": "合成用户", "city": "北京"}),))
        cx.execute("INSERT INTO application_queue VALUES('q1','legacy-one','job1','submitted')")
        cx.execute("INSERT INTO sessions VALUES('must-not-import-token')")
    destination = tmp_path / "frozen-snapshot.sqlite3"
    backup_sqlite(source, destination)
    return destination


def test_dry_run_requires_no_postgres_and_never_claims_migration(tmp_path):
    frozen = snapshot(tmp_path)
    report = import_snapshot(None, frozen)
    assert not report["applied"] and report["counts"]["resumes"] == 1
    assert report["unmapped_tables"] == ["unsupported_feature"]
    assert "login_sessions" in report["not_imported"]
    with pytest.raises(ValueError):
        import_snapshot(None, frozen, apply=True)
    with pytest.raises(ValueError):
        backup_sqlite(frozen, frozen)


def test_migration_refuses_to_merge_into_nonempty_database(v2_db, tmp_path):
    Repository(v2_db).ensure_user("migration-existing-" + uuid.uuid4().hex)
    with pytest.raises(Conflict):
        import_snapshot(v2_db, snapshot(tmp_path), apply=True, frozen=True)


@pytest.fixture
def empty_import_db(v2_db):
    if os.getenv("ZHIDA_TEST_ALLOW_CREATE_DATABASE") != "1":
        pytest.skip("Explicit permission to create a synthetic migration database is required")
    name = "zhida_test_import_" + uuid.uuid4().hex
    assert name.startswith("zhida_test_import_") and name.isidentifier()
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
    database = Database(url)
    try:
        yield database
    finally:
        database.close()
        # Only the UUID-named database created by this fixture is removed.
        with v2_db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as cx:
            cx.exec_driver_sql('DROP DATABASE "' + name + '"')


def test_real_offline_import_preserves_versions_archive_and_unknown_confirmation(empty_import_db, tmp_path):
    frozen = snapshot(tmp_path)
    report = import_snapshot(empty_import_db, frozen, apply=True, frozen=True)
    assert report["mapped_resumes"] == 1 and report["archived_records"] == 3
    assert import_snapshot(empty_import_db, frozen, apply=True, frozen=True)["already_imported"]
    repo = Repository(empty_import_db)
    assert len(repo.list_resumes("legacy-one")) == 1
    assert repo.facts("legacy-one") == []
    assert len(repo.facts("legacy-one", include_unconfirmed=True)) == 2
    with empty_import_db.transaction("legacy-one") as cx:
        assert cx.execute(text("SELECT count(*) FROM zhida.auth_sessions")).scalar() == 0
        assert cx.execute(text("SELECT count(*) FROM zhida.legacy_records")).scalar() == 3
        assert cx.execute(text("SELECT count(*) FROM zhida.background_jobs WHERE kind='rag.index'")).scalar() == 1
