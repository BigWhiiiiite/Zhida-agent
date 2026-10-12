"""Own only this project's seven host processes; never migrate or open a browser."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from urllib.request import ProxyHandler, build_opener

from native_backend_v2 import (BACKEND, ROOT as INFRA, command_identity, load_state,
    operation_lock, port_available, private_write, read_environment, running, save_state)

ROOT = BACKEND / ".v2-runtime" / "workbench-app"
KEYS = {"DATABASE_URL", "BROKER_URL", "REDIS_CACHE_URL", "REDIS_CONTROL_URL", "ENVIRONMENT",
        "LOCAL_USER_ENABLED", "LOCAL_USER_ID", "COOKIE_SECURE", "FILE_ROOT", "POOL_SIZE", "WORKFLOW_ENABLED", "RECOGNITION_ENABLED", "ASSISTED_ENABLED"}
ALLOWED = {"ZHIDA_V2_" + key for key in KEYS}
KNOWN = ALLOWED | {"ZHIDA_V2_NATIVE_ADMIN_PASSWORD", "ZHIDA_V2_MIGRATION_DATABASE_URL"}


def environment(infra: Path, uploads: Path) -> dict:
    uploads = uploads.expanduser().resolve()
    if not uploads.is_dir() or uploads in {(BACKEND / "uploads").resolve(), BACKEND.resolve(), *BACKEND.resolve().parents}:
        raise ValueError("--uploads must be the existing independent migrated attachment directory")
    values = read_environment(infra)
    if set(values) - KNOWN:
        raise ValueError("Unknown V2 runtime configuration key; no child process started")
    required = {"ZHIDA_V2_" + key for key in ("DATABASE_URL", "BROKER_URL", "REDIS_CACHE_URL", "REDIS_CONTROL_URL")}
    if not all(values.get(key) for key in required):
        raise ValueError("Native runtime configuration is incomplete")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ZHIDA_V2_", "ZHIDA_TEST_"))}
    env.update({key: value for key, value in values.items() if key in ALLOWED})
    env.update(ZHIDA_STORAGE_BACKEND="postgresql", ZHIDA_UPLOAD_DIR=str(uploads),
               APP_AUTH_REQUIRED="false", PYTHONUNBUFFERED="1", NOSETPS="1")
    return env


def prepare(root: Path) -> None:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    marker = root / "prepared.json"
    if not marker.exists():
        if list(root.iterdir()):
            raise RuntimeError("Unowned application state directory; refusing to adopt it")
        private_write(marker, json.dumps({"project": str(BACKEND.resolve())}))
    if json.loads(marker.read_text()).get("project") != str(BACKEND.resolve()):
        raise RuntimeError("Application state belongs to another project")
    if root.stat().st_mode & 0o077:
        raise RuntimeError("Application state directory must have mode 0700")
    (root / "logs").mkdir(mode=0o700, exist_ok=True)
    if (root / "logs").stat().st_mode & 0o077:
        raise RuntimeError("Application logs directory must have mode 0700")


def commands(python: Path) -> dict[str, list[str]]:
    base = [str(python), "-m", "app.backend_v2.cli"]
    return {"gateway": [str(python), "-m", "uvicorn", "app.backend_v2.gateway:app", "--host", "127.0.0.1", "--port", "18000"],
        **{queue: [*base, "worker", "--queue", queue, "--concurrency", "1", "--pool", "solo"]
           for queue in ("workflow.advance", "document.parse", "rag.index", "maintenance")},
        "publisher": [*base, "publisher"], "scheduler": [*base, "scheduler"]}


def healthy() -> bool:
    try:
        with build_opener(ProxyHandler({})).open("http://127.0.0.1:18000/api/health", timeout=1) as response:
            payload = json.load(response)
        return payload.get("status") == "ok" and payload.get("storage") == "postgresql" and payload.get("backend_generation") == "v2"
    except Exception:
        return False


def status(root: Path = ROOT) -> dict:
    state = load_state(root)
    processes = {name: "running" if running(record) else "stopped_or_identity_changed" for name, record in state.items()}
    return {"processes": processes, "gateway_healthy": processes.get("gateway") == "running" and healthy(),
            "note": "Process liveness is not queue-consumption or browser-fill verification"}


def start(infra: Path, uploads: Path, python: Path, root: Path = ROOT, *, enable_assisted=False) -> dict:
    prepare(root)
    python = python.expanduser().absolute()  # Preserve the venv symlink, not its base interpreter.
    env, plans = environment(infra, uploads), commands(python)
    if enable_assisted:
        env.update(ZHIDA_V2_WORKFLOW_ENABLED='true',ZHIDA_V2_RECOGNITION_ENABLED='true',ZHIDA_V2_ASSISTED_ENABLED='true')
    fingerprint = hashlib.sha256(json.dumps({key: env[key] for key in sorted(ALLOWED | {"ZHIDA_UPLOAD_DIR"}) if key in env}, sort_keys=True).encode()).hexdigest()
    if not python.is_file():
        raise ValueError("V2 Python executable not found")
    with operation_lock(root):
        state = load_state(root)
        alive = {name for name, record in state.items() if running(record)}
        if alive:
            binding = root / "launch-fingerprint"
            if alive == set(plans) and binding.exists() and binding.read_text() == fingerprint and healthy():
                return {"result": "already_running", **status(root)}
            raise RuntimeError("Partial or unhealthy existing application; inspect status/logs then stop before restart")
        if not port_available(18000):
            raise RuntimeError("Port 18000 is occupied by an unverified service; nothing started")
        pending = root / "launch-fingerprint.pending"
        private_write(pending, fingerprint)
        pending.replace(root / "launch-fingerprint")
        for name, command in plans.items():
            path = root / "logs" / (name + ".log")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "ab") as log:
                process = subprocess.Popen(command, cwd=BACKEND, env=env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, start_new_session=True)
            try:
                time.sleep(.15)
            finally:
                record = {"pid": process.pid, "identity": command_identity(process.pid)}
                state[name] = record
                save_state(root, state)
            if not record["identity"] or process.poll() is not None:
                raise RuntimeError(f"{name} failed to start; inspect private logs, other started processes retained")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if all(running(record) for record in state.values()) and healthy():
                return {"result": "started", **status(root)}
            time.sleep(.25)
        raise RuntimeError("Processes started but health was not verified; inspect status/private logs, do not start twice")


def stop(root: Path = ROOT, timeout: float = 5.0) -> dict:
    prepare(root)
    with operation_lock(root):
        state, result, pending = load_state(root), {}, {}
        for name, record in state.items():
            if running(record):
                try:
                    os.kill(record["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    result[name] = "exited_before_signal"
                    continue
                result[name], pending[name] = "termination_requested; still_running", record
            else:
                result[name] = "not_running_or_identity_changed; not_signalled"
        deadline = time.monotonic() + min(max(timeout, 0), 10)
        while pending:
            for name, record in list(pending.items()):
                if not running(record):
                    result[name] = "exited_or_identity_changed; no_further_signal"
                    del pending[name]
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(.1)
        return {"result": result, "data_and_logs": "retained"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("--infra-root", type=Path, default=INFRA)
    parser.add_argument("--uploads", type=Path)
    parser.add_argument("--python", type=Path, default=BACKEND / ".venv-v2" / "bin" / "python")
    parser.add_argument('--enable-assisted',action='store_true',help='Enable migrated D3 graph and local Runner')
    args = parser.parse_args()
    if args.action == "start" and args.uploads is None:
        parser.error("start requires --uploads pointing to the migrated independent directory")
    try:
        result = start(args.infra_root, args.uploads, args.python,enable_assisted=args.enable_assisted) if args.action == "start" else stop() if args.action == "stop" else status()
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"Application manager failed: {exc}\n")


if __name__ == "__main__":
    main()
