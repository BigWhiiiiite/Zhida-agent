"""Manage the isolated local v2 stack without reading the legacy backend .env.

init creates random local-only credentials. start brings up infrastructure;
start --application also builds the new API and workers and runs migrations.
stop preserves every named volume. This tool never resets or imports legacy data.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys


REPO = Path(__file__).resolve().parents[2]
ENV_FILE = REPO / "backend" / ".env.v2.local"
COMPOSE = REPO / "compose.backend-v2.yml"
PORTS = {
    "POSTGRES": 15432,
    "REDIS_CACHE": 16379,
    "REDIS_CONTROL": 16380,
    "RABBITMQ": 15674,
    "RABBITMQ_MANAGEMENT": 15672,
    "API": 18000,
}


def initialize() -> None:
    db_password, app_password, redis_password, broker_password = (secrets.token_hex(24) for _ in range(4))
    values = {
        "ZHIDA_V2_ENVIRONMENT": "development",
        "ZHIDA_V2_POSTGRES_PASSWORD": db_password,
        "ZHIDA_V2_APP_DATABASE_PASSWORD": app_password,
        "ZHIDA_V2_REDIS_PASSWORD": redis_password,
        "ZHIDA_V2_RABBITMQ_PASSWORD": broker_password,
        **{f"ZHIDA_V2_{name}_PORT": str(port) for name, port in PORTS.items()},
        "ZHIDA_V2_DATABASE_URL": f"postgresql+psycopg://zhida_runtime:{app_password}@127.0.0.1:15432/zhida_v2",
        "ZHIDA_V2_MIGRATION_DATABASE_URL": f"postgresql+psycopg://zhida_migrator:{db_password}@127.0.0.1:15432/zhida_v2",
        "ZHIDA_V2_REDIS_CACHE_URL": f"redis://:{redis_password}@127.0.0.1:16379/0",
        "ZHIDA_V2_REDIS_CONTROL_URL": f"redis://:{redis_password}@127.0.0.1:16380/0",
        "ZHIDA_V2_BROKER_URL": f"amqp://zhida:{broker_password}@127.0.0.1:15674/zhida",
        "ZHIDA_V2_LOCAL_USER_ENABLED": "false",
        "ZHIDA_V2_LOCAL_USER_ID": "local-development-user",
        "ZHIDA_V2_COOKIE_SECURE": "false",
    }
    try:
        descriptor = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("backend/.env.v2.local already exists; it has not been overwritten.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("# Generated local development credentials; do not commit.\n")
        stream.writelines(f"{key}={value}\n" for key, value in values.items())
    print("Created backend/.env.v2.local with private permissions. Credentials were not printed.")


def environment() -> dict[str, str]:
    if not ENV_FILE.is_file():
        raise SystemExit("Run 'backend_v2_stack.py init' first.")
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, separator, value = line.partition("=")
            if not separator or not key.startswith("ZHIDA_V2_"):
                raise SystemExit("Invalid entry in backend/.env.v2.local; expected ZHIDA_V2_NAME=value.")
            values[key] = value
    return values


def compose_command(arguments: list[str], *, application: bool = False) -> int:
    environment()
    docker = shutil.which("docker")
    if not docker:
        raise SystemExit("Docker is unavailable. Start/install a Docker-compatible engine, or use the documented native test setup.")
    command = [docker, "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE)]
    if application:
        command += ["--profile", "application"]
    return subprocess.call(command + arguments, cwd=REPO)


def doctor() -> int:
    docker = shutil.which("docker")
    data: dict[str, object] = {
        "python": sys.version.split()[0],
        "python_supported": (3, 11) <= sys.version_info[:2] <= (3, 12),
        "docker_command": bool(docker),
        "local_environment_file": ENV_FILE.is_file(),
    }
    if docker:
        try:
            probe = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"],
                                   capture_output=True, text=True, timeout=8)
            data["docker_engine_available"] = probe.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            data["docker_engine_available"] = False
    else:
        data["docker_engine_available"] = False
    values = environment() if ENV_FILE.is_file() else {}
    statuses = {}
    for name, default in PORTS.items():
        port = int(values.get(f"ZHIDA_V2_{name}_PORT", default))
        with socket.socket() as probe:
            probe.settimeout(.25)
            occupied = probe.connect_ex(("127.0.0.1", port)) == 0
        statuses[name.lower()] = {"host": "127.0.0.1", "port": port, "listening": occupied}
    data["ports"] = statuses
    data["note"] = "Listening only indicates a TCP listener, not authentication, migration, or integration success."
    print(json.dumps(data, indent=2))
    return 0 if data["python_supported"] and data["docker_engine_available"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "doctor", "start", "status", "stop", "validate"))
    parser.add_argument("--application", action="store_true", help="Include the v2 API, four workers, publisher, scheduler and migrations.")
    args = parser.parse_args()
    if args.command == "init":
        initialize()
        return 0
    if args.command == "doctor":
        return doctor()
    commands = {
        "start": ["up", "--detach", "--build", "--wait", "--wait-timeout", "180"],
        "status": ["ps", "--all"],
        "stop": ["stop"],
        "validate": ["config", "--quiet"],
    }
    return compose_command(commands[args.command], application=args.application)


if __name__ == "__main__":
    raise SystemExit(main())
