"""Run a temporary loopback RabbitMQ fixture using explicitly supplied binaries.

No existing node, broker data or credentials are loaded. Stop with Ctrl-C; only
the child processes started here are terminated, and diagnostic files remain.
This is a test fixture, not a service installer or production launcher.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile
import time


def stop_requested(signum, frame):
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rabbitmq-server", type=Path, required=True)
    parser.add_argument("--erlang-bin", type=Path, required=True)
    parser.add_argument("--port", type=int, default=15674)
    parser.add_argument("--epmd-port", type=int, default=14371)
    parser.add_argument("--distribution-port", type=int, default=25674)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    for port in (args.port, args.epmd_port, args.distribution_port):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    directory = args.directory or Path(tempfile.mkdtemp(prefix="zhida-rabbitmq-test-"))
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise SystemExit("Fixture directory must be empty; no existing data was changed.")
    directory.chmod(0o700)
    password, cookie = secrets.token_hex(24), secrets.token_hex(24)
    (directory / "rabbitmq.conf").write_text(
        f"listeners.tcp.default = 127.0.0.1:{args.port}\n"
        "default_user = zhida_test\n"
        f"default_pass = {password}\n"
        "default_vhost = zhida_test\n"
        "vm_memory_high_watermark.absolute = 192MiB\n"
        "disk_free_limit.absolute = 64MB\n", encoding="utf-8")
    (directory / "enabled_plugins").write_text("[].\n", encoding="utf-8")
    (directory / "fixture.env").write_text(
        f"ZHIDA_V2_BROKER_URL=amqp://zhida_test:{password}@127.0.0.1:{args.port}/zhida_test\n",
        encoding="utf-8")
    (directory / "fixture.env").chmod(0o600)
    env = os.environ.copy()
    # Do not inherit other brokers' RABBITMQ_* configuration or inspect HOME.
    for key in list(env):
        if key.startswith("RABBITMQ_") or key in {"ERL_FLAGS", "ERL_AFLAGS", "ERL_ZFLAGS", "ERL_LIBS"}:
            env.pop(key)
    env.update({
        "PATH": str(args.erlang_bin) + os.pathsep + env.get("PATH", ""),
        "ERL_EPMD_ADDRESS": "127.0.0.1", "ERL_EPMD_PORT": str(args.epmd_port),
        "RABBITMQ_NODENAME": f"zhida_v2_test_{os.getpid()}@localhost",
        "RABBITMQ_CONFIG_FILE": str(directory / "rabbitmq.conf"),
        "RABBITMQ_CONF_ENV_FILE": str(directory / "unused-env.conf"),
        "RABBITMQ_ENABLED_PLUGINS_FILE": str(directory / "enabled_plugins"),
        "RABBITMQ_MNESIA_BASE": str(directory / "mnesia"),
        "RABBITMQ_LOG_BASE": str(directory / "logs"),
        "RABBITMQ_PID_FILE": str(directory / "rabbitmq.pid"),
        "RABBITMQ_PLUGINS_EXPAND_DIR": str(directory / "plugins"),
        "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS": (
            f"+S 2:2 -setcookie {cookie} -kernel inet_dist_use_interface {{127,0,0,1}} "
            f"inet_dist_listen_min {args.distribution_port} inet_dist_listen_max {args.distribution_port}"
        ),
    })
    erlang_root = args.erlang_bin.parent / "lib" / "erlang"
    if erlang_root.is_dir():
        env["ERL_ROOTDIR"] = str(erlang_root)
    children: list[subprocess.Popen] = []
    signal.signal(signal.SIGTERM, stop_requested)
    log = (directory / "fixture.log").open("wb")
    try:
        children.append(subprocess.Popen([str(args.erlang_bin / "epmd"), "-port", str(args.epmd_port)], env=env, stdout=log, stderr=log))
        children.append(subprocess.Popen([str(args.rabbitmq_server)], env=env, stdout=log, stderr=log, start_new_session=True))
        print(f"Starting isolated broker. Test environment file: {directory / 'fixture.env'}", flush=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                raise RuntimeError(f"A fixture process exited; inspect {directory / 'fixture.log'}")
            with socket.socket() as probe:
                probe.settimeout(.2)
                ready = probe.connect_ex(("127.0.0.1", args.port)) == 0
            if ready:
                print(f"AMQP TCP listener ready on 127.0.0.1:{args.port}; tests must still verify authentication and delivery.", flush=True)
                break
            time.sleep(.2)
        else:
            raise RuntimeError(f"Broker startup timed out; inspect {directory / 'fixture.log'}")
        while all(child.poll() is None for child in children):
            time.sleep(1)
        raise RuntimeError("A fixture process stopped unexpectedly.")
    except KeyboardInterrupt:
        print("Stopping this fixture's child processes only.", flush=True)
        return 0
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
