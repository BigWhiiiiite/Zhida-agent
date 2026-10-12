"""Backup, verify, then explicitly import the existing workbench to PostgreSQL.

Staging is read-only for the original SQLite and uploads. Apply requires the
operator to stop the old API; this tool never stops arbitrary processes itself.
No attachment is uploaded and no model is called.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backend_v2.legacy_import import backup_sqlite
from app.backend_v2.workbench_import import attachment_manifest, import_snapshot, inspect_snapshot


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(piece)
    return result.hexdigest()


def write_private(path, value):
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)


def source_fingerprint(source):
    wal = Path(str(source) + "-wal")
    return {"main": checksum(source), "wal": checksum(wal) if wal.exists() else None}


def stage(source, uploads, destination, unowned_user=None, *, frozen=False):
    if not frozen:
        raise ValueError("请先停止旧后端写入，再明确 --confirm-old-api-stopped")
    source, uploads = source.resolve(strict=True), uploads.resolve(strict=True)
    destination = destination.absolute()
    if destination.resolve().is_relative_to(uploads):
        raise ValueError("暂存目录不能放在原附件目录中")
    if destination.exists():
        raise ValueError("暂存目标已存在；不会覆盖之前的迁移备份")
    before = source_fingerprint(source)
    destination.mkdir(parents=True, mode=0o700)
    destination.chmod(0o700)
    snapshot = destination / "resumes.snapshot.sqlite3"
    backup_sqlite(source, snapshot)
    preview, _ = inspect_snapshot(snapshot, unowned_user=unowned_user)
    if preview["unknown_tables"] or preview["unknown_columns"]:
        raise ValueError("旧数据库包含未映射业务数据，已保留快照，停止迁移")
    manifest = attachment_manifest(snapshot, uploads, unowned_user=unowned_user)
    if manifest["missing"] or manifest["mismatch"]:
        raise ValueError("附件缺失或校验不符，已保留快照；不会切换到不完整数据")
    target = destination / "uploads"
    target.mkdir(mode=0o700)
    copied = set()
    for record in manifest["files"]:
        name = record["stored_filename"]
        if name in copied:
            continue
        original, final = uploads / name, target / name
        if original.is_symlink():
            raise ValueError("附件在备份期间变化，停止迁移")
        with original.open("rb") as reader, final.open("xb") as writer:
            final.chmod(0o600)
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
        if checksum(final) != record["sha256"]:
            raise ValueError("附件复制校验失败；不会继续迁移")
        copied.add(name)
    after = source_fingerprint(source)
    if before != after:
        raise ValueError("旧库在备份期间改变；已保留暂存文件但不会允许导入，请停止写入后重新暂存")
    receipt = {"created_at": datetime.now(timezone.utc).isoformat(),
        "original_database": str(source), "source_hash": checksum(source),
        "source_fingerprint": before,
        "snapshot": str(snapshot), "snapshot_hash": checksum(snapshot),
        "uploads": str(target), "unowned_user": unowned_user,
        "preview": preview, "attachments": manifest}
    write_private(destination / "staging.json", receipt)
    return {"staging": str(destination), "counts": preview["counts"],
            "attachments_verified": len(copied), "status": "staged_not_imported"}


def apply(staging: Path, *, frozen: bool):
    if not frozen:
        raise ValueError("请先停止旧后端写入，再明确 --confirm-old-api-stopped")
    manifest_path = staging.resolve(strict=True) / "staging.json"
    saved = json.loads(manifest_path.read_text())
    snapshot, source = Path(saved["snapshot"]), Path(saved["original_database"])
    if checksum(snapshot) != saved["snapshot_hash"] or source_fingerprint(source) != saved["source_fingerprint"]:
        raise ValueError("旧库或快照在备份后改变，请重新暂存，不覆盖既有备份")
    if Path(str(source) + "-wal").exists() and Path(str(source) + "-wal").stat().st_size:
        raise ValueError("旧库仍有 WAL；请关闭写入并重新建立一致快照")
    for record in saved["attachments"]["files"]:
        path = Path(saved["uploads"]) / record["stored_filename"]
        if path.is_symlink() or checksum(path) != record["sha256"]:
            raise ValueError("暂存附件已改变，停止迁移")
    from app.backend_v2.db import Database
    url = os.environ.get("ZHIDA_V2_MIGRATION_DATABASE_URL") or os.environ.get("ZHIDA_V2_DATABASE_URL", "")
    db = Database(url)
    try:
        result = import_snapshot(db, snapshot, apply=True, frozen=True, unowned_user=saved.get("unowned_user"))
    finally:
        db.close()
    report_path = staging / ("migration-" + uuid.uuid4().hex[:12] + ".json")
    write_private(report_path, result)
    return {"status": "imported_verified", "records": result["imported_records"],
            "counts": result["counts"], "uploads": saved["uploads"], "report": str(report_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("stage")
    preparation.add_argument("--source", type=Path, required=True)
    preparation.add_argument("--uploads", type=Path, required=True)
    preparation.add_argument("--destination", type=Path, required=True)
    preparation.add_argument("--unowned-user")
    preparation.add_argument("--confirm-old-api-stopped", action="store_true")
    migration = commands.add_parser("apply")
    migration.add_argument("--staging", type=Path, required=True)
    migration.add_argument("--confirm-old-api-stopped", action="store_true")
    args = parser.parse_args()
    if args.command == "stage":
        result = stage(args.source, args.uploads, args.destination, args.unowned_user,
                       frozen=args.confirm_old_api_stopped)
    else:
        result = apply(args.staging, frozen=args.confirm_old_api_stopped)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
