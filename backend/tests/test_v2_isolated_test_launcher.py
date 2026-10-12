"""Safety checks for the synthetic-only launcher; no services or credentials."""
import importlib.util
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("isolated_launcher_test", Path(__file__).resolve().parents[1] / "scripts/test_v2_isolated.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def targets():
    return {"ZHIDA_TEST_DATABASE_URL": "postgresql+psycopg://test:synthetic@127.0.0.1:15432/zhida_test_fixture",
            "ZHIDA_TEST_BROKER_URL": "amqp://test:synthetic@127.0.0.1:15674/zhida_test_fixture"}


@pytest.mark.parametrize("key,value", [
    ("ZHIDA_TEST_DATABASE_URL", "postgresql+psycopg://test@127.0.0.1/zhida_v2"),
    ("ZHIDA_TEST_DATABASE_URL", "postgresql+psycopg://test@elsewhere/zhida_test_fixture"),
    ("ZHIDA_TEST_DATABASE_URL", "postgresql+psycopg://test@127.0.0.1/zhida_test_fixture?host=elsewhere"),
    ("ZHIDA_TEST_BROKER_URL", "amqp://test@127.0.0.1/zhida"),
    ("ZHIDA_TEST_BROKER_URL", "amqp://test@elsewhere/zhida_test_fixture"),
])
def test_launcher_rejects_business_and_nonlocal_targets(key, value):
    values = targets()
    values[key] = value
    with pytest.raises(ValueError, match="loopback"):
        launcher.validate_test_targets(values)


def test_old_environment_redis_and_implicit_optins_never_forwarded(tmp_path):
    path = tmp_path / "tests.env"
    values = targets() | {"ZHIDA_TEST_REDIS_URL": "redis://:private@127.0.0.1:16380/0",
                          "ZHIDA_TEST_REAL_EMBEDDING": "1", "ZHIDA_TEST_ALLOW_CREATE_DATABASE": "1"}
    launcher.private_write(path, "\n".join(f"{key}={value}" for key, value in values.items()))
    assert launcher.read_test_environment(path) == targets()
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        launcher.read_test_environment(path)


@pytest.mark.parametrize("ports", [(16379, 16380), (17379, 17379), (6379, 17380), (18000, 17380)])
def test_live_or_duplicate_redis_ports_rejected(ports):
    with pytest.raises(ValueError, match="dedicated"):
        launcher.validate_ports(*ports)


def test_test_environment_scrubs_real_credentials_and_redirects_all_services(monkeypatch, tmp_path):
    monkeypatch.setenv("ZHIDA_V2_DATABASE_URL", "production")
    monkeypatch.setenv("ZHIDA_V2_REDIS_CACHE_URL", "production-cache")
    monkeypatch.setenv("OPENAI_API_KEY", "never-forward")
    monkeypatch.setenv("TASK_TEST_PRESERVED", "unchanged")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    env = launcher.test_process_environment(targets(), "isolated-cache", "isolated-control", tmp_path,
                                            real_embedding=False, create_database=False)
    assert env["TASK_TEST_PRESERVED"] == "unchanged"
    assert env["LANGSMITH_TRACING"] == env["LANGCHAIN_TRACING_V2"] == "false"
    assert "OPENAI_API_KEY" not in env
    assert env["ZHIDA_V2_DATABASE_URL"] == targets()["ZHIDA_TEST_DATABASE_URL"]
    assert env["ZHIDA_STORAGE_BACKEND"] == "postgresql"
    assert env["ZHIDA_V2_REDIS_CONTROL_URL"] == env["ZHIDA_TEST_REDIS_URL"] == "isolated-control"
    assert env["ZHIDA_V2_REDIS_CACHE_URL"] == env["ZHIDA_TEST_CACHE_REDIS_URL"] == "isolated-cache"
    assert env["ZHIDA_TEST_REAL_EMBEDDING"] == env["ZHIDA_TEST_ALLOW_CREATE_DATABASE"] == "0"
    assert env["PYTHON_DOTENV_DISABLED"] == env["HF_HUB_OFFLINE"] == "1"


def test_shutdown_only_touches_own_child_and_waits():
    process = Mock()
    process.poll.return_value = None
    launcher.stop_owned_process(process)
    process.terminate.assert_called_once()
    process.kill.assert_not_called()
    process.wait.assert_called_once_with(timeout=5)
    finished = Mock()
    finished.poll.return_value = 0
    launcher.stop_owned_process(finished)
    finished.terminate.assert_not_called()


def test_stuck_own_child_is_killed_after_bounded_grace():
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("synthetic-child", 5), 0]
    launcher.stop_owned_process(process)
    process.kill.assert_called_once()


def test_suite_deadline_reports_timeout_and_stops_only_owned_pytest(capsys):
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired('synthetic-pytest', 2), 0]
    assert launcher.wait_for_tests(process, 2) == 124
    process.terminate.assert_called_once()
    assert 'exceeded 2s' in capsys.readouterr().err
