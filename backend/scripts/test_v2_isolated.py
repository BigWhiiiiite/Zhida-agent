"""Run synthetic V2 tests without borrowing the workbench's Redis instances.

Uses only an explicitly provisioned zhida_test* database and RabbitMQ vhost.
Two private, ephemeral Redis child processes are always created. Credentials
are never printed or passed as command-line arguments. No live .env is loaded.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time

from sqlalchemy.engine import make_url

BACKEND = Path(__file__).resolve().parents[1]
NATIVE = BACKEND / ".v2-runtime" / "native"
TEST_KEYS = {
    "ZHIDA_TEST_DATABASE_URL", "ZHIDA_TEST_BROKER_URL", "ZHIDA_TEST_REDIS_URL",
    "ZHIDA_TEST_CACHE_REDIS_URL", "ZHIDA_TEST_REAL_EMBEDDING", "ZHIDA_TEST_ALLOW_CREATE_DATABASE",
}


def read_test_environment(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Test credentials must be a regular, private 0600 file")
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition("=")
        if not sep or key not in TEST_KEYS or key in values:
            raise ValueError("Unexpected or duplicated test environment entry")
        parsed = shlex.split(raw)
        if len(parsed) != 1:
            raise ValueError("Invalid test environment value")
        values[key] = parsed[0]
    # Intentionally discard both legacy Redis URLs, even if they look like test
    # URLs: older synthetic-tests.env files point to the real app's Redis.
    return validate_test_targets(values)


def validate_test_targets(values: dict[str, str]) -> dict[str, str]:
    result = {}
    for key, schemes in (
        ("ZHIDA_TEST_DATABASE_URL", {"postgresql", "postgresql+psycopg"}),
        ("ZHIDA_TEST_BROKER_URL", {"amqp", "amqps"}),
    ):
        try:
            url = make_url(values[key])
        except Exception:
            raise ValueError("Both synthetic database and broker must be configured") from None
        if (url.drivername not in schemes or url.host != "127.0.0.1" or url.query
                or not re.fullmatch(r"zhida_test_[a-zA-Z0-9_]+", url.database or "")):
            raise ValueError("Only loopback zhida_test_* database and broker targets are accepted")
        result[key] = values[key]
    return result


def validate_ports(cache_port: int, control_port: int) -> None:
    if cache_port == control_port or any(not 17000 <= port <= 17999 for port in (cache_port, control_port)):
        raise ValueError("Use two distinct dedicated test ports in 17000..17999")


def ensure_free_ports(cache_port: int, control_port: int) -> None:
    validate_ports(cache_port, control_port)
    for port in (cache_port, control_port):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"Test port {port} is occupied; no existing service will be adopted or stopped") from None


def private_write(path: Path, content: str) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(content)


def test_process_environment(values: dict[str, str], cache_url: str, control_url: str,
                             root: Path, *, real_embedding: bool, create_database: bool) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if (key.startswith(("ZHIDA_", "OPENAI_", "ANTHROPIC_", "ISRC_", "AWS_", "CELERY_", "RABBITMQ_",
                            "LANGSMITH_", "LANGCHAIN_"))
                or key.endswith(("API_KEY", "ACCESS_TOKEN"))):
            env.pop(key)
    env.update(values)
    env.update(
        ZHIDA_TEST_REDIS_URL=control_url, ZHIDA_TEST_CACHE_REDIS_URL=cache_url,
        ZHIDA_TEST_REAL_EMBEDDING="1" if real_embedding else "0",
        ZHIDA_TEST_ALLOW_CREATE_DATABASE="1" if create_database else "0",
        ZHIDA_V2_DATABASE_URL=values["ZHIDA_TEST_DATABASE_URL"],
        ZHIDA_V2_BROKER_URL=values["ZHIDA_TEST_BROKER_URL"],
        ZHIDA_V2_REDIS_CACHE_URL=cache_url, ZHIDA_V2_REDIS_CONTROL_URL=control_url,
        ZHIDA_V2_ENVIRONMENT="isolated-test", ZHIDA_V2_LOCAL_USER_ENABLED="false",
        ZHIDA_STORAGE_BACKEND="postgresql",
        ZHIDA_V2_FILE_ROOT=str(root / "documents"), ZHIDA_UPLOAD_DIR=str(root / "uploads"),
        PYTHON_DOTENV_DISABLED="1", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1",
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", NOSETPS="1",
        LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false",
    )
    return env


def stop_owned_process(process: subprocess.Popen) -> None:
    """Never load saved PIDs or signal an existing service; only our child."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait_for_tests(process: subprocess.Popen, timeout: int) -> int:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"Synthetic test suite exceeded {timeout}s; stopping only its owned process.", file=sys.stderr)
        return 124
    finally:
        stop_owned_process(process)


def interrupt_for_cleanup(_signal, _frame) -> None:
    raise KeyboardInterrupt


@contextmanager
def isolated_redis(binary: Path, root: Path, cache_port: int, control_port: int):
    import redis

    ensure_free_ports(cache_port, control_port)
    password = secrets.token_hex(24)
    processes = []
    streams = []
    clients = []
    try:
        for name, port, policy in (("cache", cache_port, "allkeys-lfu"), ("control", control_port, "noeviction")):
            directory = root / name
            directory.mkdir(mode=0o700)
            config = directory / "redis.conf"
            private_write(config, f"bind 127.0.0.1\nport {port}\nprotected-mode yes\n"
                          f"requirepass {password}\ndir {json.dumps(str(directory), ensure_ascii=False)}\n"
                          f"maxmemory 64mb\nmaxmemory-policy {policy}\nsave \"\"\n"
                          "appendonly no\ndaemonize no\n")
            log = os.fdopen(os.open(directory / "redis.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
            streams.append(log)
            process = subprocess.Popen([str(binary), str(config)], stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            url = f"redis://:{password}@127.0.0.1:{port}/0"
            client = redis.Redis.from_url(url, socket_connect_timeout=0.3, socket_timeout=0.3)
            clients.append(client)
            deadline = time.monotonic() + 5
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Dedicated test Redis {name} failed to start")
                try:
                    if client.ping():
                        break
                except redis.RedisError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Dedicated test Redis {name} health check failed")
                time.sleep(0.05)
        yield (f"redis://:{password}@127.0.0.1:{cache_port}/0", f"redis://:{password}@127.0.0.1:{control_port}/0")
    finally:
        for process in reversed(processes):
            stop_owned_process(process)
        for client in clients:
            client.close()
        for stream in streams:
            stream.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-env", type=Path, default=NATIVE / "synthetic-tests.env")
    parser.add_argument("--redis-server", type=Path, default=NATIVE / "bin" / "redis-server")
    parser.add_argument("--cache-port", type=int, default=17379)
    parser.add_argument("--control-port", type=int, default=17380)
    parser.add_argument("--real-embedding", action="store_true", help="Opt in to local cached embedding only (offline)")
    parser.add_argument("--allow-create-database", action="store_true", help="Allow synthetic migration tests to create zhida_test_* databases")
    parser.add_argument("--timeout", type=int, default=300, help="Whole-suite deadline in seconds (1..3600)")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    values = read_test_environment(args.test_env)
    validate_ports(args.cache_port, args.control_port)
    if not 1 <= args.timeout <= 3600:
        raise ValueError("Test timeout must be between 1 and 3600 seconds")
    if not args.redis_server.is_file() or not os.access(args.redis_server, os.X_OK):
        raise ValueError("Explicit Redis binary must exist and be executable")
    # SIGTERM should execute the same owned-child cleanup as Ctrl-C. SIGKILL
    # cannot be handled; occupied-port checks still prevent later adoption.
    signal.signal(signal.SIGTERM, interrupt_for_cleanup)
    # Even separate Redis processes still share the synthetic DB/vhost. Refuse
    # simultaneous wrapper runs, rather than let workers consume each other's jobs.
    with os.fdopen(os.open(args.test_env.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another isolated suite owns this synthetic database/vhost") from None
        with tempfile.TemporaryDirectory(prefix="zhida-v2-tests-") as temporary:
            root = Path(temporary)
            with isolated_redis(args.redis_server.resolve(), root, args.cache_port, args.control_port) as (cache, control):
                env = test_process_environment(values, cache, control, root,
                                               real_embedding=args.real_embedding,
                                               create_database=args.allow_create_database)
                pytest_args = args.pytest_args
                if pytest_args[:1] == ["--"]:
                    pytest_args = pytest_args[1:]
                print(f"Synthetic tests: isolated database/vhost; owned Redis {args.cache_port}/{args.control_port}; no live .env.", flush=True)
                process = subprocess.Popen([sys.executable, "-m", "pytest", *(pytest_args or ["-q", "tests"])], cwd=BACKEND, env=env)
                return wait_for_tests(process, args.timeout)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
