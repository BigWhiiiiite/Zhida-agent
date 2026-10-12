"""No sockets or actual processes: validate the native manager's safety contract."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location("native_v2", Path(__file__).resolve().parents[1] / "scripts" / "native_backend_v2.py")
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


def package_fixtures(tmp_path):
    sources = {name: tmp_path / "packages" / name for name in ("postgres", "rabbitmq", "erlang", "openssl")}
    for name, relative in (("postgres", "bin/initdb"), ("postgres", "share/postgresql/extension/vector.control"),
                           ("rabbitmq", "libexec/rabbitmq-server"), ("erlang", "bin/erl"), ("openssl", "lib/readme")):
        path = sources[name] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic executable fixture")
    sources["redis"] = tmp_path / "redis-server"
    sources["redis"].write_text("synthetic executable fixture")
    return sources


def test_prepare_creates_private_persistent_configs_without_starting(tmp_path, monkeypatch):
    root = tmp_path / "native"
    sources = package_fixtures(tmp_path)
    monkeypatch.setattr(native.sys, "platform", "darwin")
    relocate = Mock()
    monkeypatch.setattr(native, "relocate_binaries", relocate)
    process = Mock(side_effect=AssertionError("prepare must not launch services"))
    monkeypatch.setattr(native.subprocess, "Popen", process)
    native.prepare(root, **sources)
    relocate.assert_called_once_with(root)
    values = native.read_environment(root)
    assert values["ZHIDA_V2_DATABASE_URL"].startswith("postgresql+psycopg://zhida_runtime:")
    assert values["ZHIDA_V2_LOCAL_USER_ENABLED"] == "false"
    assert "ZHIDA_V2_NATIVE_ADMIN_PASSWORD" not in values
    assert "ZHIDA_V2_MIGRATION_DATABASE_URL" not in values
    assert "ZHIDA_V2_NATIVE_ADMIN_PASSWORD" in native.read_environment(root, bootstrap=True)
    assert (root / "runtime.env").stat().st_mode & 0o777 == 0o600
    assert (root / "rabbitmq/.erlang.cookie").stat().st_mode & 0o777 == 0o600
    assert "bind 127.0.0.1" in (root / "redis-control/redis.conf").read_text()
    assert "appendonly yes" in (root / "redis-control/redis.conf").read_text()
    assert "noeviction" in (root / "redis-control/redis.conf").read_text()
    assert "allkeys-lfu" in (root / "redis-cache/redis.conf").read_text()
    assert "scram-sha-256" in (root / "postgres/postgresql.conf").read_text()
    assert not (root / "postgres/data").exists()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        native.prepare(root, **sources)


def test_insecure_environment_is_rejected(tmp_path):
    path = tmp_path / "runtime.env"
    native.private_write(path, "ZHIDA_V2_DATABASE_URL=synthetic\n")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        native.read_environment(tmp_path)


def test_stop_never_signals_reused_pid(tmp_path, monkeypatch):
    native.private_write(tmp_path / "prepared.json", "{}")
    native.save_state(tmp_path, {"postgres": {"pid": 54321, "identity": "old process signature"}})
    monkeypatch.setattr(native, "command_identity", lambda pid: "unrelated new process")
    signal = Mock(side_effect=AssertionError("reused PID must never be signalled"))
    monkeypatch.setattr(native.os, "kill", signal)
    result = native.stop(tmp_path)
    assert result["postgres"] == "not_running_or_identity_changed; not_signalled"
    signal.assert_not_called()


def test_start_refuses_unowned_port_before_mutation(tmp_path, monkeypatch):
    native.private_write(tmp_path / "prepared.json", "{}")
    monkeypatch.setattr(native, "port_available", lambda port: False)
    monkeypatch.setattr(native, "command_identity", lambda pid: "")
    process = Mock(side_effect=AssertionError("occupied port must fail before startup"))
    monkeypatch.setattr(native.subprocess, "Popen", process)
    with pytest.raises(RuntimeError, match="occupied by an unverified service"):
        native.start(tmp_path)
    process.assert_not_called()


def test_clean_environment_does_not_repurpose_home_or_inherit_broker(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", "/synthetic-user-home")
    monkeypatch.setenv("RABBITMQ_NODENAME", "unrelated")
    monkeypatch.setenv("ERL_FLAGS", "-setcookie secret")
    monkeypatch.setenv("PGPASSWORD", "unrelated")
    monkeypatch.setenv("ZHIDA_V2_DATABASE_URL", "unrelated")
    result = native.clean_environment(tmp_path)
    assert result["HOME"] == "/synthetic-user-home"
    assert not {"RABBITMQ_NODENAME", "ERL_FLAGS", "PGPASSWORD", "ZHIDA_V2_DATABASE_URL"}.intersection(result)


def test_rabbit_environment_cookie_is_file_not_command_line(tmp_path):
    native.private_write(tmp_path / "prepared.json", json.dumps({"node": "synthetic@localhost"}))
    (tmp_path / "rabbitmq").mkdir()
    native.private_write(tmp_path / "rabbitmq/.erlang.cookie", "synthetic-test-cookie")
    env = native.rabbit_environment(tmp_path)
    assert "-setcookie" not in env["RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS"]
    assert "-args_file " + str(tmp_path / "rabbitmq/erlang.args") in env["RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS"]
    assert (tmp_path / "rabbitmq/erlang.args").stat().st_mode & 0o777 == 0o600
    assert "synthetic-test-cookie" not in str(env)
    assert env["ERL_EPMD_ADDRESS"] == "127.0.0.1"
    assert env["ERL_ROOTDIR"] == str(tmp_path / "bin/erlang/lib/erlang")


def test_package_shell_paths_are_relocated_not_original_changed(tmp_path, monkeypatch):
    libexec = tmp_path / "bin/rabbitmq/libexec"
    libexec.mkdir(parents=True)
    (libexec / "rabbitmq-env").write_text("#!/bin/sh\nRABBITMQ_HOME=/private/tmp/old\n")
    (libexec / "rabbitmq-defaults").write_text("SYS_PREFIX=/private/tmp/old\nCLEAN_BOOT_FILE=old\nSASL_BOOT_FILE=old\n")
    native.relocate_binaries(tmp_path)
    assert "/private/tmp/old" not in (libexec / "rabbitmq-env").read_text()
    assert str(tmp_path / "bin/rabbitmq") in (libexec / "rabbitmq-env").read_text()
    assert str(tmp_path / "bin/erlang/lib/erlang/bin/start_clean") in (libexec / "rabbitmq-defaults").read_text()


def test_atomic_state_preserves_pending_for_diagnostics(tmp_path):
    native.private_write(tmp_path / "processes.json.pending", "interrupted update")
    with pytest.raises(RuntimeError, match="Incomplete state update"):
        native.save_state(tmp_path, {})
    assert (tmp_path / "processes.json.pending").read_text() == "interrupted update"


def test_chinese_runtime_directory_is_literal_not_json_escaped(tmp_path, monkeypatch):
    root = tmp_path / "暑期实习" / "native"
    sources = package_fixtures(tmp_path)
    monkeypatch.setattr(native.sys, "platform", "darwin")
    monkeypatch.setattr(native, "relocate_binaries", lambda root: None)
    native.prepare(root, **sources)
    config = (root / "redis-cache/redis.conf").read_text()
    assert str(root / "redis-cache") in config
    assert "\\u6691" not in config


def test_compose_bootstrap_grants_workbench_future_tables():
    script = (Path(__file__).resolve().parents[1] / "scripts/postgres_v2_roles.sh").read_text()
    assert "CREATE SCHEMA IF NOT EXISTS workbench" in script
    assert 'ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA zhida, rag, workbench' in script
    assert "ON ALL TABLES IN SCHEMA zhida, rag, workbench" in script


def test_credential_split_keeps_app_env_unprivileged_and_preserves_values(tmp_path):
    values = {"ZHIDA_V2_DATABASE_URL": "postgresql://runtime@local/app", "ZHIDA_V2_NATIVE_ADMIN_PASSWORD": "synthetic-admin",
              "ZHIDA_V2_MIGRATION_DATABASE_URL": "postgresql://migration@local/app"}
    native.private_write(tmp_path / "runtime.env", native.serialize_environment(values))
    native.isolate_credentials(tmp_path)
    assert native.read_environment(tmp_path) == {"ZHIDA_V2_DATABASE_URL": values["ZHIDA_V2_DATABASE_URL"]}
    assert native.read_environment(tmp_path, bootstrap=True) == values
    assert (tmp_path / "bootstrap.env").stat().st_mode & 0o777 == 0o600
    native.isolate_credentials(tmp_path)


def test_port_probe_handles_time_wait_but_does_not_allow_shared_listeners(monkeypatch):
    probe = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=probe)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(native.socket, "socket", lambda: context)
    assert native.port_available(15432)
    probe.setsockopt.assert_called_once_with(native.socket.SOL_SOCKET, native.socket.SO_REUSEADDR, 1)
    probe.bind.assert_called_once_with(("127.0.0.1", 15432))
