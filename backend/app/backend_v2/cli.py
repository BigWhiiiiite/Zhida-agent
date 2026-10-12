"""Explicit V2 process and ingestion commands; never imports the old backend."""
import argparse
import json
import os
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Zhida A-C backend")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--queue", required=True, choices=("workflow.advance", "document.parse", "rag.index", "maintenance"))
    worker.add_argument("--concurrency", type=int, default=1)
    worker.add_argument("--pool", choices=("solo", "prefork"), default="solo" if sys.platform == "darwin" else "prefork")
    for kind in ("publisher", "scheduler"):
        service = commands.add_parser(kind)
        service.add_argument("--once", action="store_true")
        service.add_argument("--interval", type=float, default=2)
    ingest = commands.add_parser("ingest-document")
    ingest.add_argument("--user", required=True)
    ingest.add_argument("--file", type=Path, required=True)
    backup = commands.add_parser("backup-sqlite")
    backup.add_argument("--source", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    legacy = commands.add_parser("import-sqlite")
    legacy.add_argument("--snapshot", type=Path, required=True)
    legacy.add_argument("--unowned-user")
    legacy.add_argument("--apply", action="store_true")
    legacy.add_argument("--confirm-frozen-snapshot", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "worker":
        if not os.getenv("ZHIDA_V2_DATABASE_URL") or not os.getenv("ZHIDA_V2_BROKER_URL"):
            parser.error("Set ZHIDA_V2_DATABASE_URL and ZHIDA_V2_BROKER_URL explicitly")
        if not 1 <= args.concurrency <= 4 or (args.pool == "solo" and args.concurrency != 1):
            parser.error("Concurrency is 1..4; solo must be 1")
        from .workers import celery_app
        celery_app.worker_main(["worker", "--loglevel=INFO", "-Q", args.queue, "-c", str(args.concurrency),
                                "--pool", args.pool, "--hostname", args.queue + "@%h",
                                "--without-gossip", "--without-mingle"])
    elif args.command in {"publisher", "scheduler"}:
        from .workers import main as service_main
        return service_main([args.command, "--interval", str(args.interval)] + (["--once"] if args.once else []))
    elif args.command == "ingest-document":
        from .db import Database
        from .documents import DocumentStore
        from .settings import Settings
        db = Database(Settings.from_env().database_url)
        try:
            print(json.dumps(DocumentStore(db).ingest(args.user, args.file), ensure_ascii=False))
        finally:
            db.close()
    elif args.command == "backup-sqlite":
        from .legacy_import import backup_sqlite
        print(json.dumps(backup_sqlite(args.source, args.destination), ensure_ascii=False))
    elif args.command == "import-sqlite":
        from .legacy_import import import_snapshot
        from .db import Database
        from .settings import Settings
        db = Database(Settings.from_env().database_url) if args.apply else None
        try:
            print(json.dumps(import_snapshot(db, args.snapshot, apply=args.apply,
                 frozen=args.confirm_frozen_snapshot, unowned_user=args.unowned_user), ensure_ascii=False))
        finally:
            if db:
                db.close()


if __name__ == "__main__":
    main()
