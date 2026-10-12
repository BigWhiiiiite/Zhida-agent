"""Application manager tests: synthetic env/processes, no sockets or services."""
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    SPEC = importlib.util.spec_from_file_location("workbench_manager", SCRIPTS / "workbench_v2_app.py")
    manager = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(manager)
finally:
    sys.path.pop(0)


@pytest.fixture
def setup(tmp_path):
    infra, uploads, root = tmp_path / "infra", tmp_path / "attachments", tmp_path / "app"
    infra.mkdir(); uploads.mkdir()
    values = {"ZHIDA_V2_DATABASE_URL": "postgresql+psycopg://runtime:synthetic-db-secret@127.0.0.1/db",
              "ZHIDA_V2_REDIS_CACHE_URL": "redis://:synthetic-cache@127.0.0.1:16379/0",
              "ZHIDA_V2_REDIS_CONTROL_URL": "redis://:synthetic-control@127.0.0.1:16380/0",
              "ZHIDA_V2_BROKER_URL": "amqp://test:synthetic-broker@127.0.0.1:15674/test",
              "ZHIDA_V2_NATIVE_ADMIN_PASSWORD": "synthetic-admin",
              "ZHIDA_V2_MIGRATION_DATABASE_URL": "synthetic-migrator"}
    path = infra / "runtime.env"
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items())); path.chmod(0o600)
    python = tmp_path / "python"
    python.write_text("synthetic interpreter")
    return infra, uploads, python, root


def test_environment_only_passes_runtime_credentials(setup, monkeypatch):
    infra, uploads, _, _ = setup
    monkeypatch.setenv("ZHIDA_V2_NATIVE_ADMIN_PASSWORD", "inherited-admin")
    monkeypatch.setenv("ZHIDA_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("HOME", "/synthetic-home")
    env = manager.environment(infra, uploads)
    assert env["HOME"] == "/synthetic-home"
    assert env["ZHIDA_STORAGE_BACKEND"] == "postgresql"
    assert env["ZHIDA_UPLOAD_DIR"] == str(uploads)
    assert env["APP_AUTH_REQUIRED"] == "false"
    assert "ZHIDA_V2_NATIVE_ADMIN_PASSWORD" not in env
    assert "ZHIDA_V2_MIGRATION_DATABASE_URL" not in env
    with pytest.raises(ValueError, match="independent"):
        manager.environment(infra, manager.BACKEND)
    (infra / "runtime.env").chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        manager.environment(infra, uploads)


def test_unknown_environment_key_rejected(setup):
    infra, uploads, _, _ = setup
    with (infra / "runtime.env").open("a") as stream:
        stream.write("ZHIDA_V2_UNRECOGNIZED=unexpected\n")
    with pytest.raises(ValueError, match="Unknown"):
        manager.environment(infra, uploads)


def test_occupied_port_does_not_start_or_adopt(setup, monkeypatch):
    infra, uploads, python, root = setup
    monkeypatch.setattr(manager, "port_available", lambda port: False)
    popen = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(manager.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="occupied"):
        manager.start(infra, uploads, python, root)
    popen.assert_not_called()


def test_start_owns_seven_processes_private_journal_and_no_secrets(setup, monkeypatch):
    infra, uploads, python, root = setup
    monkeypatch.setattr(manager, "port_available", lambda port: True)
    monkeypatch.setattr(manager, "command_identity", lambda pid: f"2026 test command {pid}")
    monkeypatch.setattr(manager, "running", lambda record: bool(record.get("identity")))
    monkeypatch.setattr(manager, "healthy", lambda: True)
    monkeypatch.setattr(manager.time, "sleep", lambda seconds: None)
    popen = Mock(side_effect=[Mock(pid=i, poll=lambda: None) for i in range(9000, 9007)])
    monkeypatch.setattr(manager.subprocess, "Popen", popen)
    assert manager.start(infra, uploads, python, root)["result"] == "started"
    assert popen.call_count == 7
    recorded = manager.load_state(root)
    assert set(recorded) == set(manager.commands(python))
    journal = (root / "processes.json").read_text()
    assert "secret" not in journal and "synthetic-db" not in journal
    assert (root / "processes.json").stat().st_mode & 0o777 == 0o600
    assert (root / "logs").stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (root / "logs").glob("*.log"))
    assert manager.start(infra, uploads, python, root)["result"] == "already_running"
    assert popen.call_count == 7  # No duplicate Workers on repeated start.
    other_uploads = uploads.parent / "other-uploads"
    other_uploads.mkdir()
    with pytest.raises(RuntimeError, match="Partial"):
        manager.start(infra, other_uploads, python, root)
    assert popen.call_count == 7
    for call in popen.call_args_list:
        assert "synthetic-db-secret" not in str(call.args)
        assert call.kwargs["start_new_session"] is True
        assert "ZHIDA_V2_NATIVE_ADMIN_PASSWORD" not in call.kwargs["env"]


def test_partial_start_refuses_duplicate_and_retains_logs(setup, monkeypatch):
    infra, uploads, python, root = setup
    manager.prepare(root)
    manager.save_state(root, {"gateway": {"pid": 9000, "identity": "owned"}})
    monkeypatch.setattr(manager, "running", lambda record: True)
    process = Mock(side_effect=AssertionError("must not duplicate"))
    monkeypatch.setattr(manager.subprocess, "Popen", process)
    with pytest.raises(RuntimeError, match="Partial"):
        manager.start(infra, uploads, python, root)
    process.assert_not_called()


def test_failed_health_never_claims_success(setup, monkeypatch):
    infra, uploads, python, root = setup
    monkeypatch.setattr(manager, "port_available", lambda port: True)
    monkeypatch.setattr(manager, "command_identity", lambda pid: "owned")
    monkeypatch.setattr(manager, "running", lambda record: True)
    monkeypatch.setattr(manager, "healthy", lambda: False)
    monkeypatch.setattr(manager.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(manager.time, "monotonic", Mock(side_effect=[0, 16]))
    monkeypatch.setattr(manager.subprocess, "Popen", Mock(side_effect=[Mock(pid=i, poll=lambda: None) for i in range(9000, 9007)]))
    with pytest.raises(RuntimeError, match="health was not verified"):
        manager.start(infra, uploads, python, root)
    assert len(manager.load_state(root)) == 7


def test_stop_signals_only_verified_identity(setup, monkeypatch):
    _, _, _, root = setup
    manager.prepare(root)
    manager.save_state(root, {"gateway": {"pid": 9000, "identity": "owned"},
                              "rag.index": {"pid": 9001, "identity": "reused"}})
    monkeypatch.setattr(manager, "running", lambda record: record["identity"] == "owned")
    kill = Mock()
    monkeypatch.setattr(manager.os, "kill", kill)
    result = manager.stop(root)
    kill.assert_called_once_with(9000, manager.signal.SIGTERM)
    assert result["result"]["rag.index"].endswith("not_signalled")
    assert (root / "prepared.json").exists()
    assert (root / "processes.json").exists()


def test_foreign_state_directory_never_adopted(setup):
    *_, root = setup
    root.mkdir()
    (root / "unrelated-data").write_text("preserve")
    with pytest.raises(RuntimeError, match="Unowned"):
        manager.prepare(root)
