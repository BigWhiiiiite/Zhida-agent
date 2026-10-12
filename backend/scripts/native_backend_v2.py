"""Explicit, project-local macOS infrastructure without Docker or system installs.

prepare copies caller-selected binaries, generates new credentials and configuration;
start initializes a NEW dedicated PostgreSQL cluster then launches four services;
stop only signals processes whose PID AND command match our recorded identity.
No SQLite files, legacy .env, user resumes or other services are accessed.
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
import shutil
import signal
import socket
import subprocess
import sys
import time

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND / ".v2-runtime" / "native"
PORTS = {"postgres": 15432, "redis_cache": 16379, "redis_control": 16380,
         "rabbitmq": 15674, "epmd": 14372, "distribution": 25675}


def private_write(path: Path, content: str) -> None:
    """Never replace existing credentials or configuration implicitly."""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(content)


def read_environment(root: Path, *, bootstrap: bool = False) -> dict[str, str]:
    result = {}
    paths = [root / "runtime.env"] + ([root / "bootstrap.env"] if bootstrap else [])
    for path in paths:
        if path.stat().st_mode & 0o077:
            raise ValueError(path.name + " must have mode 0600; credentials were not read")
        for line in path.read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not key.startswith("ZHIDA_V2_"):
                raise ValueError("Unexpected native environment entry")
            result[key] = shlex.split(value)[0]
    return result


def serialize_environment(values: dict[str, str]) -> str:
    return "# Dedicated native credentials; do not commit or print.\n" + "".join(
        f"{key}={shlex.quote(value)}\n" for key, value in values.items())


def isolate_credentials(root: Path) -> None:
    """One-way local configuration upgrade, never prints or rotates secrets."""
    values = read_environment(root)
    keys = ("ZHIDA_V2_NATIVE_ADMIN_PASSWORD", "ZHIDA_V2_MIGRATION_DATABASE_URL")
    bootstrap = {key: values.pop(key) for key in keys if key in values}
    if not bootstrap:
        return
    private_write(root / "bootstrap.env", serialize_environment(bootstrap))
    pending = root / "runtime.env.pending"
    private_write(pending, serialize_environment(values))
    pending.replace(root / "runtime.env")


def clean_environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("ZHIDA_V2_", "RABBITMQ_", "PG")) or key in {
            "ERL_FLAGS", "ERL_AFLAGS", "ERL_ZFLAGS", "ERL_LIBS", "ERL_ROOTDIR", "ERL_EPMD_PORT", "ERL_EPMD_ADDRESS",
        }:
            env.pop(key)
    # HOME remains unchanged; no unrelated service configuration is loaded.
    return env


def command_identity(pid: int) -> str:
    if pid <= 1:
        return ""
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def load_state(root: Path) -> dict:
    path = root / "processes.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(root: Path, state: dict) -> None:
    pending = root / "processes.json.pending"
    if pending.exists():
        raise RuntimeError("Incomplete state update; inspect processes.json.pending before continuing")
    private_write(pending, json.dumps(state))
    pending.replace(root / "processes.json")


def running(record: dict) -> bool:
    identity = command_identity(int(record.get("pid", 0)))
    return bool(identity) and identity == record.get("identity")


@contextmanager
def operation_lock(root: Path):
    if not (root / "prepared.json").is_file():
        raise RuntimeError("Run prepare first; this manager never adopts another installation")
    descriptor = os.open(root / "manager.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise RuntimeError("Another native start/stop is in progress") from None
    finally:
        os.close(descriptor)


def relocate_binaries(root: Path) -> None:
    """Relocate private copies only, including prior test bottles' /tmp paths."""
    rabbit = root / "bin" / "rabbitmq"
    erlang = root / "bin" / "erlang" / "lib" / "erlang"
    shell = rabbit / "libexec" / "rabbitmq-env"
    source = shell.read_text()
    source, count = re.subn(r"^RABBITMQ_HOME=.*$", "RABBITMQ_HOME=" + shlex.quote(str(rabbit)), source, flags=re.M)
    if count != 1:
        raise ValueError("Unsupported RabbitMQ package: expected one RABBITMQ_HOME assignment")
    shell.chmod(shell.stat().st_mode | 0o200)
    shell.write_text(source)
    defaults = rabbit / "libexec" / "rabbitmq-defaults"
    source = defaults.read_text()
    for key, value in {"SYS_PREFIX": root, "CLEAN_BOOT_FILE": erlang / "bin" / "start_clean",
                       "SASL_BOOT_FILE": erlang / "bin" / "start_clean"}.items():
        source, count = re.subn(r"^" + key + r"=.*$", key + "=" + shlex.quote(str(value)), source, flags=re.M)
        if count != 1:
            raise ValueError("Unsupported RabbitMQ defaults: " + key)
    defaults.chmod(defaults.stat().st_mode | 0o200)
    defaults.write_text(source)
    # Erlang crypto NIFs in relocated Homebrew bottles can embed /tmp libcrypto.
    # Rewrite only OpenSSL references, never user/system libraries. Re-sign copied files.
    for binary in [*(root / "bin" / "erlang").rglob("*.so"), *(root / "bin" / "openssl" / "lib").glob("*.dylib")]:
        if binary.is_symlink():
            continue
        dependencies = subprocess.check_output(["otool", "-L", str(binary)], text=True).splitlines()[1:]
        changed = False
        for line in dependencies:
            original = line.strip().split(" (", 1)[0]
            if Path(original).name.startswith(("libcrypto.", "libssl.")) and original.startswith("/"):
                target = root / "bin" / "openssl" / "lib" / Path(original).name
                if not target.is_file():
                    raise ValueError("Copied OpenSSL does not contain required " + target.name)
                relative = "@loader_path/" + os.path.relpath(target, binary.parent)
                binary.chmod(binary.stat().st_mode | 0o200)
                subprocess.run(["install_name_tool", "-change", original, relative, str(binary)], check=True,
                               capture_output=True)
                changed = True
        if changed:
            subprocess.run(["codesign", "--force", "--sign", "-", str(binary)], check=True, capture_output=True)


def prepare(root: Path, *, postgres: Path, redis: Path, rabbitmq: Path, erlang: Path, openssl: Path) -> None:
    if sys.platform != "darwin":
        raise ValueError("Native packaging is macOS-only; use Compose on other platforms")
    if any(ch.isspace() for ch in str(root)):
        raise ValueError("This RabbitMQ package requires a runtime path without whitespace")
    required = [postgres / "bin" / "initdb", postgres / "share" / "postgresql" / "extension" / "vector.control",
                redis, rabbitmq / "libexec" / "rabbitmq-server", erlang / "bin" / "erl", openssl / "lib"]
    if not all(path.exists() for path in required):
        raise ValueError("Explicit binary paths must contain PostgreSQL + pgvector, Redis, RabbitMQ, Erlang and OpenSSL")
    if root.exists():
        raise ValueError("Native directory already exists; refusing to overwrite data or credentials")
    root.mkdir(parents=True, mode=0o700)
    (root / "bin").mkdir(mode=0o700)
    for name, source in {"postgres": postgres, "rabbitmq": rabbitmq, "erlang": erlang, "openssl": openssl}.items():
        shutil.copytree(source.resolve(), root / "bin" / name, symlinks=False)
    shutil.copy2(redis.resolve(), root / "bin" / "redis-server")
    relocate_binaries(root)
    for directory in ("postgres", "redis-cache", "redis-control", "rabbitmq", "logs"):
        (root / directory).mkdir(mode=0o700)
    admin, migration, runtime, redis_password, broker_password = [secrets.token_hex(24) for _ in range(5)]
    values = {
        "ZHIDA_V2_ENVIRONMENT": "development", "ZHIDA_V2_NATIVE_ADMIN_PASSWORD": admin,
        "ZHIDA_V2_DATABASE_URL": f"postgresql+psycopg://zhida_runtime:{runtime}@127.0.0.1:15432/zhida_v2",
        "ZHIDA_V2_MIGRATION_DATABASE_URL": f"postgresql+psycopg://zhida_migrator:{migration}@127.0.0.1:15432/zhida_v2",
        "ZHIDA_V2_REDIS_CACHE_URL": f"redis://:{redis_password}@127.0.0.1:16379/0",
        "ZHIDA_V2_REDIS_CONTROL_URL": f"redis://:{redis_password}@127.0.0.1:16380/0",
        "ZHIDA_V2_BROKER_URL": f"amqp://zhida:{broker_password}@127.0.0.1:15674/zhida",
        "ZHIDA_V2_LOCAL_USER_ENABLED": "false", "ZHIDA_V2_LOCAL_USER_ID": "local-development-user",
        "ZHIDA_V2_COOKIE_SECURE": "false", "ZHIDA_V2_FILE_ROOT": str(BACKEND / ".v2-runtime" / "files"),
    }
    bootstrap = {key: values.pop(key) for key in ("ZHIDA_V2_NATIVE_ADMIN_PASSWORD", "ZHIDA_V2_MIGRATION_DATABASE_URL")}
    private_write(root / "runtime.env", serialize_environment(values))
    private_write(root / "bootstrap.env", serialize_environment(bootstrap))
    private_write(root / "postgres" / "admin-password", admin + "\n")
    private_write(root / "postgres" / "postgresql.conf",
                  "listen_addresses = '127.0.0.1'\nport = 15432\nunix_socket_directories = ''\n"
                  "password_encryption = 'scram-sha-256'\nmax_connections = 80\nshared_buffers = '128MB'\n")
    for name, port, cache in (("redis-cache", PORTS["redis_cache"], True), ("redis-control", PORTS["redis_control"], False)):
        private_write(root / name / "redis.conf", f"bind 127.0.0.1\nport {port}\nprotected-mode yes\n"
                      f"requirepass {redis_password}\ndir {json.dumps(str(root / name), ensure_ascii=False)}\n"
                      f"maxmemory {'256mb' if cache else '128mb'}\nmaxmemory-policy {'allkeys-lfu' if cache else 'noeviction'}\n"
                      f"save \"\"\nappendonly {'no' if cache else 'yes'}\nappendfsync everysec\ndaemonize no\n")
    private_write(root / "rabbitmq" / "rabbitmq.conf", f"listeners.tcp.default = 127.0.0.1:{PORTS['rabbitmq']}\n"
                  f"default_user = zhida\ndefault_pass = {broker_password}\ndefault_vhost = zhida\n"
                  "vm_memory_high_watermark.absolute = 192MiB\ndisk_free_limit.absolute = 256MB\n")
    private_write(root / "rabbitmq" / "enabled_plugins", "[].\n")
    private_write(root / "rabbitmq" / ".erlang.cookie", secrets.token_hex(32))
    private_write(root / "prepared.json", json.dumps({"version": 1, "ports": PORTS,
                  "node": "zhida_native_" + secrets.token_hex(5) + "@localhost"}))


def bootstrap_postgres(root: Path) -> None:
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url
    env = read_environment(root, bootstrap=True)
    url = make_url(env["ZHIDA_V2_DATABASE_URL"])
    migration = make_url(env["ZHIDA_V2_MIGRATION_DATABASE_URL"])
    connection_args = dict(host="127.0.0.1", port=PORTS["postgres"], user="zhida_native_admin",
                           password=env["ZHIDA_V2_NATIVE_ADMIN_PASSWORD"], autocommit=True)
    with psycopg.connect(dbname="postgres", **connection_args) as cx:
        for role, password in (("zhida_migrator", migration.password), ("zhida_runtime", url.password)):
            if not cx.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                cx.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD {}")
                           .format(sql.Identifier(role), sql.Literal(password)))
        if not cx.execute("SELECT 1 FROM pg_database WHERE datname='zhida_v2'").fetchone():
            cx.execute("CREATE DATABASE zhida_v2 OWNER zhida_migrator")
    with psycopg.connect(dbname="zhida_v2", **connection_args) as cx:
        cx.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cx.execute("GRANT CONNECT ON DATABASE zhida_v2 TO zhida_runtime")
        cx.execute("GRANT USAGE ON SCHEMA public TO zhida_runtime")
        for schema in ("zhida", "rag", "workbench"):
            cx.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION zhida_migrator").format(sql.Identifier(schema)))
            cx.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO zhida_runtime").format(sql.Identifier(schema)))
            cx.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE zhida_migrator IN SCHEMA {} GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO zhida_runtime").format(sql.Identifier(schema)))
            cx.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE zhida_migrator IN SCHEMA {} GRANT USAGE,SELECT ON SEQUENCES TO zhida_runtime").format(sql.Identifier(schema)))
            cx.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO zhida_runtime").format(sql.Identifier(schema)))
            cx.execute(sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO zhida_runtime").format(sql.Identifier(schema)))


def rabbit_environment(root: Path) -> dict[str, str]:
    env = clean_environment(root)
    rabbit = root / "rabbitmq"
    arguments = rabbit / "erlang.args"
    if not arguments.exists():
        # OTP injects its own -home, so adding a second -home does not reliably
        # redirect auth. A private args file avoids both HOME changes and a cookie in ps.
        private_write(arguments, "-setcookie " + (rabbit / ".erlang.cookie").read_text().strip() + "\n")
    env.update({
        "PATH": str(root / "bin" / "erlang" / "bin") + os.pathsep + env.get("PATH", ""),
        "ERL_ROOTDIR": str(root / "bin" / "erlang" / "lib" / "erlang"),
        "ERL_EPMD_ADDRESS": "127.0.0.1", "ERL_EPMD_PORT": str(PORTS["epmd"]),
        "RABBITMQ_NODENAME": json.loads((root / "prepared.json").read_text())["node"],
        "RABBITMQ_SCRIPTS_DIR": str(root / "bin" / "rabbitmq" / "libexec"),
        "RABBITMQ_CONFIG_FILE": str(rabbit / "rabbitmq.conf"),
        "RABBITMQ_CONF_ENV_FILE": str(rabbit / "unused-env.conf"),
        "RABBITMQ_ENABLED_PLUGINS_FILE": str(rabbit / "enabled_plugins"),
        "RABBITMQ_MNESIA_BASE": str(rabbit / "mnesia"), "RABBITMQ_LOG_BASE": str(rabbit / "logs"),
        "RABBITMQ_PID_FILE": str(rabbit / "rabbitmq.pid"),
        "RABBITMQ_PLUGINS_EXPAND_DIR": str(rabbit / "plugins"),
        "RABBITMQ_CTL_ERL_ARGS": f"+S 2:2 -args_file {arguments}",
        # Forces exec to beam, so the saved PID remains the actual service PID.
        "RUNNING_UNDER_SYSTEMD": "true",
        "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS": (
            f"+S 2:2 -args_file {arguments} -kernel inet_dist_use_interface {{127,0,0,1}} "
            f"inet_dist_listen_min {PORTS['distribution']} inet_dist_listen_max {PORTS['distribution']}"),
    })
    return env


def port_available(port: int) -> bool:
    try:
        with socket.socket() as probe:
            # Immediate restart can see TIME_WAIT after a clean shutdown; that
            # is not an active foreign listener. Never enable SO_REUSEPORT.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def spawn(root: Path, name: str, command: list[str], env: dict, state: dict, port: int) -> None:
    with (root / "logs" / f"{name}.log").open("ab") as log:
        child = subprocess.Popen(command, env=env, cwd=root, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=log, start_new_session=True)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"{name} exited; inspect its private log (credentials are not printed)")
            if not port_available(port):
                identity = command_identity(child.pid)
                if identity:
                    state[name] = {"pid": child.pid, "identity": identity}
                    save_state(root, state)
                    return
            time.sleep(.2)
        raise RuntimeError(f"{name} startup timed out; process retained for diagnostics, use stop")
    except BaseException:
        # Include interrupted/slow children in the journal; never blindly restart them.
        if child.poll() is None:
            state[name] = {"pid": child.pid, "identity": command_identity(child.pid)}
            save_state(root, state)
        raise


def start(root: Path) -> None:
    with operation_lock(root):
        state = load_state(root)
        for name, port in PORTS.items():
            if not port_available(port) and not running(state.get(name, {})):
                # Distribution belongs to our verified Rabbit process, not its own PID.
                if name == "distribution" and running(state.get("rabbitmq", {})):
                    continue
                raise RuntimeError(f"Port {port} is occupied by an unverified service; nothing was replaced")
        env = clean_environment(root)
        pg = root / "bin" / "postgres" / "bin"
        data = root / "postgres" / "data"
        if not (data / "PG_VERSION").exists():
            subprocess.run([str(pg / "initdb"), "-D", str(data), "-U", "zhida_native_admin",
                            "--auth-host=scram-sha-256", "--auth-local=reject", "--encoding=UTF8", "--locale=C",
                            "--pwfile=" + str(root / "postgres" / "admin-password")], check=True, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not running(state.get("postgres", {})):
            spawn(root, "postgres", [str(pg / "postgres"), "-D", str(data),
                  "-c", "config_file=" + str(root / "postgres" / "postgresql.conf")], env, state, PORTS["postgres"])
        # Authenticated query, not just open port, is required before progressing.
        import psycopg
        deadline = time.monotonic() + 15
        while True:
            try:
                bootstrap_postgres(root)
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
        for name in ("redis_cache", "redis_control"):
            if not running(state.get(name, {})):
                spawn(root, name, [str(root / "bin" / "redis-server"), str(root / name.replace("_", "-") / "redis.conf")],
                      env, state, PORTS[name])
        rabbit_env = rabbit_environment(root)
        if not running(state.get("epmd", {})):
            spawn(root, "epmd", [str(root / "bin" / "erlang" / "bin" / "epmd"), "-port", str(PORTS["epmd"])],
                  rabbit_env, state, PORTS["epmd"])
        if not running(state.get("rabbitmq", {})):
            spawn(root, "rabbitmq", [str(root / "bin" / "rabbitmq" / "libexec" / "rabbitmq-server")],
                  rabbit_env, state, PORTS["rabbitmq"])
        health(root)


def health(root: Path) -> dict:
    import psycopg
    import redis
    from kombu import Connection
    from sqlalchemy.engine import make_url
    env = read_environment(root)
    url = make_url(env["ZHIDA_V2_DATABASE_URL"])
    with psycopg.connect(host=url.host, port=url.port, dbname=url.database, user=url.username,
                         password=url.password, connect_timeout=4) as cx:
        row = cx.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
        if row != ("zhida_runtime", False, False):
            raise RuntimeError("Runtime database role is not safely restricted")
    for key in ("ZHIDA_V2_REDIS_CACHE_URL", "ZHIDA_V2_REDIS_CONTROL_URL"):
        client = redis.Redis.from_url(env[key], socket_connect_timeout=4, socket_timeout=4)
        try:
            client.ping()
        finally:
            client.close()
    with Connection(env["ZHIDA_V2_BROKER_URL"], connect_timeout=4) as connection:
        connection.connect()
    return {"database_authentication": True, "runtime_rls_role": True,
            "redis_cache_authentication": True, "redis_control_authentication": True,
            "rabbitmq_authentication": True, "application_health_checked": False,
            "application_status_command": "scripts/workbench_v2_app.py status"}


def stop(root: Path) -> dict:
    with operation_lock(root):
        state = load_state(root)
        result = {}
        for name in ("rabbitmq", "epmd", "redis_control", "redis_cache", "postgres"):
            record = state.get(name)
            if not record:
                continue
            if not running(record):
                result[name] = "not_running_or_identity_changed; not_signalled"
                continue
            os.kill(record["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 20
            while running(record) and time.monotonic() < deadline:
                time.sleep(.1)
            result[name] = "still_stopping" if running(record) else "stopped; data_preserved"
        return result


def test_resources(root: Path) -> Path:
    """Create separate synthetic-only DB/vhost, never rename the business database."""
    import psycopg
    from sqlalchemy.engine import make_url
    target = root / "synthetic-tests.env"
    if target.exists():
        raise ValueError("Synthetic test configuration exists; refusing to overwrite")
    env = read_environment(root, bootstrap=True)
    database_name = "zhida_test_native"
    admin_password = env["ZHIDA_V2_NATIVE_ADMIN_PASSWORD"]
    connection_args = dict(host="127.0.0.1", port=PORTS["postgres"], user="zhida_native_admin",
                           password=admin_password, autocommit=True)
    with psycopg.connect(dbname="zhida_v2", **connection_args) as cx:
        if cx.execute("SELECT to_regclass('zhida.users')").fetchone()[0] and cx.execute("SELECT EXISTS(SELECT 1 FROM zhida.users)").fetchone()[0]:
            raise ValueError("Business data is already present; create fully separate test services instead")
    with psycopg.connect(dbname="postgres", **connection_args) as cx:
        if cx.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database_name,)).fetchone():
            raise ValueError("Synthetic database already exists; inspect it instead of adopting it")
        cx.execute("CREATE DATABASE zhida_test_native OWNER zhida_native_admin")
    with psycopg.connect(dbname=database_name, **connection_args) as cx:
        cx.execute("CREATE EXTENSION IF NOT EXISTS vector")
    broker_env = rabbit_environment(root)
    ctl = str(root / "bin/rabbitmq/libexec/rabbitmqctl")
    subprocess.run([ctl, "add_vhost", database_name], env=broker_env, check=True, capture_output=True, timeout=30)
    subprocess.run([ctl, "set_permissions", "-p", database_name, "zhida", ".*", ".*", ".*"],
                   env=broker_env, check=True, capture_output=True, timeout=30)
    broker = make_url(env["ZHIDA_V2_BROKER_URL"]).set(database=database_name)
    values = {
        "ZHIDA_TEST_DATABASE_URL": f"postgresql+psycopg://zhida_native_admin:{admin_password}@127.0.0.1:15432/{database_name}",
        "ZHIDA_TEST_BROKER_URL": broker.render_as_string(hide_password=False),
        "ZHIDA_TEST_REDIS_URL": env["ZHIDA_V2_REDIS_CONTROL_URL"],
        "ZHIDA_TEST_CACHE_REDIS_URL": env["ZHIDA_V2_REDIS_CACHE_URL"],
        "ZHIDA_TEST_REAL_EMBEDDING": "1", "ZHIDA_TEST_ALLOW_CREATE_DATABASE": "1",
    }
    private_write(target, "# Only use before real cutover; Redis instances shared with still-empty native stack.\n" +
                  "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "start", "status", "health", "stop", "test-resources", "isolate-credentials"))
    for name in ("postgres-prefix", "redis-server", "rabbitmq-prefix", "erlang-prefix", "openssl-prefix"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            values = [args.postgres_prefix, args.redis_server, args.rabbitmq_prefix, args.erlang_prefix, args.openssl_prefix]
            if not all(values):
                parser.error("prepare requires five explicit binary/package paths")
            prepare(ROOT, postgres=values[0], redis=values[1], rabbitmq=values[2], erlang=values[3], openssl=values[4])
            print("Prepared isolated native binaries/configuration; no services or user data migration started.")
        elif args.command == "start":
            start(ROOT)
            print(json.dumps(health(ROOT)))
        elif args.command == "health":
            print(json.dumps(health(ROOT)))
        elif args.command == "status":
            state = load_state(ROOT)
            print(json.dumps({name: {"pid": row.get("pid"), "verified_running": running(row)} for name, row in state.items()}))
        elif args.command == "test-resources":
            with operation_lock(ROOT):
                print("Created separate synthetic database/vhost. Private test environment: " + str(test_resources(ROOT)))
        elif args.command == "isolate-credentials":
            with operation_lock(ROOT):
                isolate_credentials(ROOT)
            print("Runtime and bootstrap credentials are separate; no passwords printed or changed.")
        else:
            print(json.dumps(stop(ROOT)))
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # Driver exceptions may embed credentials; keep details in process-local diagnostics.
        print(f"Native operation failed ({type(exc).__name__}); inspect private logs. No data was deleted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
