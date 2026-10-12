"""Full, explicit offline workbench migration, with row-for-row verification.

No source file is modified; sessions and derived caches are deliberately reset.
Unknown business tables/columns block apply rather than disappear silently.
The returned report contains counts/hashes only, never resume/account contents.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3

from sqlalchemy import text

from .repository import Conflict
from .workbench_schema import CACHE_TABLES, IMPORT_TABLES, PRIVATE_TABLES, WORKBENCH_TABLES

MAX_SNAPSHOT_BYTES = 1024 * 1024 * 1024
MAX_TABLE_ROWS = 200_000
SKIP_TABLES = CACHE_TABLES | {"sessions", "sqlite_sequence"}


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _columns(table: str) -> set[str]:
    return set(re.findall(r"(?:^|,)\s*([a-z_]+)\s+(?:TEXT|INTEGER|BIGINT|DOUBLE|tsvector)\b",
                          WORKBENCH_TABLES[table], re.I))


def _rows_digest(rows: list[dict]) -> str:
    canonical = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows)
    return hashlib.sha256("\n".join(canonical).encode()).hexdigest()


def inspect_snapshot(snapshot: Path, *, unowned_user: str | None = None) -> tuple[dict, dict]:
    snapshot = snapshot.resolve(strict=True)
    if not snapshot.is_file() or snapshot.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError("快照不是文件或超过 1 GiB，请先单独评估")
    if Path(str(snapshot) + "-wal").exists():
        raise ValueError("不能迁移有 WAL 的在线数据库；请先创建一致性备份快照")
    if unowned_user is not None and not unowned_user.strip():
        raise ValueError("无归属资料的目标用户不能为空")
    records: dict[str, list[dict]] = {}
    mismatches = {}
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as cx:
        cx.row_factory = sqlite3.Row
        if cx.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite 快照完整性检查失败")
        tables = {row[0] for row in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"resumes", "candidate_profiles"}.issubset(tables):
            raise ValueError("快照缺少职达简历或主档案表")
        counts = {}
        for table in sorted(tables):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", table):
                raise ValueError("快照含不支持的表标识符")
            counts[table] = cx.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]
        for table in IMPORT_TABLES:
            if table not in tables:
                records[table] = []
                continue
            if counts[table] > MAX_TABLE_ROWS:
                raise ValueError("单表超过 200000 条，停止而不是截断导入")
            columns = {row["name"] for row in cx.execute('PRAGMA table_info("' + table + '")')}
            unknown_columns = columns - _columns(table)
            if unknown_columns:
                mismatches[table] = sorted(unknown_columns)
            rows = [dict(row) for row in cx.execute('SELECT * FROM "' + table + '"')]
            for row in rows:
                if table in PRIVATE_TABLES and not row.get("user_id"):
                    if not unowned_user:
                        raise ValueError("存在无归属资料；请用 unowned_user 明确归属后重新预览")
                    row["user_id"] = unowned_user
                if table in {"resumes", "candidate_profiles"}:
                    if not isinstance(json.loads(row["profile_json"]), dict):
                        raise ValueError("旧简历或主档案 JSON 不是对象，停止迁移")
                if table == "application_knowledge":
                    # Keep rules/mappings exactly, but never reuse stale model
                    # vectors whose model/version no longer matches V2.
                    row["vector_model"], row["vector_json"] = "", "[]"
            records[table] = rows
    # Internal FTS5 shadow tables are cache data, not missing business records.
    unknown = tables - set(IMPORT_TABLES) - SKIP_TABLES
    unknown = {table for table in unknown if not table.startswith("rag_evidence_fts_")}
    owners = {row["user_id"] for table, rows in records.items() if table in PRIVATE_TABLES for row in rows}
    owners.update(row["id"] for row in records.get("users", []))
    resume_owners = {row["id"]: row["user_id"] for row in records["resumes"]}
    for table in {"confirmed_resume_facts", "conflicts", "application_queue"}:
        for row in records.get(table, []):
            reference = row.get("resume_id")
            if reference in resume_owners and resume_owners[reference] != row["user_id"]:
                raise ValueError("旧记录引用了另一用户的简历；停止迁移并人工核对归属")
    report = {
        "source_hash": _digest_file(snapshot), "source_counts": counts,
        "counts": {table: len(rows) for table, rows in records.items()},
        "row_hashes": {table: _rows_digest(rows) for table, rows in records.items()},
        "owners": len(owners), "unknown_tables": sorted(unknown), "unknown_columns": mismatches,
        "not_imported": ["login_sessions", "old_embedding_caches", "browser_process_state"],
        "attachment_policy": "separate_verified_manifest", "applied": False,
    }
    return report, records


def attachment_manifest(snapshot: Path, uploads_dir: Path, *, unowned_user: str | None = None) -> dict:
    """Validate referenced originals without reading PDF content or changing it.

    Paths/record IDs are for the local copying tool; public logs should show only
    summary fields. Hashing bytes locally does not parse or send resume text.
    """
    report, records = inspect_snapshot(snapshot, unowned_user=unowned_user)
    root = uploads_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("附件根目录不是文件夹")
    files, seen = [], set()
    for row in records["resumes"]:
        name = row.get("stored_filename", "")
        if not name:
            continue
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("附件记录含不安全路径，停止迁移")
        key = (row["user_id"], name)
        if key in seen:
            continue
        seen.add(key)
        path = root / name
        entry = {"resume_id": row["id"], "user_id": row["user_id"], "stored_filename": name}
        if not path.is_file():
            entry.update(status="missing")
        elif path.is_symlink() or path.resolve().parent != root:
            raise ValueError("附件不能是符号链接或跳出上传目录")
        else:
            size, checksum = path.stat().st_size, _digest_file(path)
            expected = row.get("content_hash", "")
            matches = (not row.get("file_size") or row["file_size"] == size)
            if re.fullmatch(r"[0-9a-fA-F]{64}", expected):
                matches = matches and expected.casefold() == checksum
            entry.update(status="ok" if matches else "mismatch", size=size, sha256=checksum)
        files.append(entry)
    return {"source_hash": report["source_hash"], "files": files,
            "count": len(files), "verified": sum(item["status"] == "ok" for item in files),
            "missing": sum(item["status"] == "missing" for item in files),
            "mismatch": sum(item["status"] == "mismatch" for item in files)}


def require_complete_manifest(manifest: dict) -> None:
    """Cutover must stop before replacing the old app if any original is lost."""
    files = manifest.get("files", [])
    if (manifest.get("count") != len(files) or manifest.get("verified") != len(files)
            or manifest.get("missing") or manifest.get("mismatch")
            or any(item.get("status") != "ok" for item in files)):
        raise ValueError("附件缺失或校验不一致，不能切换后端；旧文件与数据库保持不变")


def import_snapshot(db, snapshot: Path, *, apply: bool = False, frozen: bool = False,
                    unowned_user: str | None = None) -> dict:
    report, records = inspect_snapshot(snapshot, unowned_user=unowned_user)
    if not apply:
        return report
    if not frozen:
        raise ValueError("请先停止旧工作台写入并明确 frozen=True")
    if report["unknown_tables"] or report["unknown_columns"]:
        raise ValueError("发现未支持的业务表或列，不能静默丢弃；请先补迁移映射")
    owners = {row["user_id"] for table, rows in records.items() if table in PRIVATE_TABLES for row in rows}
    owners.update(row["id"] for row in records.get("users", []))
    with db.transaction() as cx:
        cx.execute(text("SELECT pg_advisory_xact_lock(194823902)"))
        previous = cx.execute(text("SELECT report FROM workbench.import_receipts WHERE source_hash=:hash"),
                              {"hash": report["source_hash"]}).scalar()
        if previous:
            return {**previous, "already_imported": True}
        # Global metadata/auth and every known owner's private rows are checked
        # with RLS active. Migrations are additionally required to use an empty
        # native account table, preventing accidental tenant merges.
        if cx.execute(text("SELECT EXISTS(SELECT 1 FROM zhida.users)")).scalar():
            raise Conflict("仅可向空的专用 V2 数据库导入，不合并已有用户")
        for table in IMPORT_TABLES:
            if table not in PRIVATE_TABLES:
                if cx.execute(text(f"SELECT EXISTS(SELECT 1 FROM workbench.{table})")).scalar():
                    raise Conflict("目标工作台已有业务数据，不能覆盖")
        verified_hashes = {}
        for table in IMPORT_TABLES:
            for row in records[table]:
                user = row.get("user_id") or ""
                cx.execute(text("SELECT set_config('zhida.user_id',:u,true)"), {"u": user})
                columns = tuple(row)
                # Fixed schema allowlist was validated above; column names are
                # still quoted, and all source values remain bound parameters.
                quoted = ",".join('"' + column + '"' for column in columns)
                placeholders = ",".join(":p" + str(index) for index in range(len(columns)))
                cx.execute(text(f"INSERT INTO workbench.{table} ({quoted}) VALUES ({placeholders})"),
                           {"p" + str(index): row[column] for index, column in enumerate(columns)})
            actual_rows = []
            if records[table]:
                source_columns = tuple(records[table][0])
                selected = ",".join('"' + column + '"' for column in source_columns)
                if table in PRIVATE_TABLES:
                    for owner in sorted(owners):
                        cx.execute(text("SELECT set_config('zhida.user_id',:u,true)"), {"u": owner})
                        actual_rows.extend(dict(row) for row in cx.execute(text(
                            f"SELECT {selected} FROM workbench.{table} WHERE user_id=:u"), {"u": owner}).mappings())
                else:
                    actual_rows = [dict(row) for row in cx.execute(text(f"SELECT {selected} FROM workbench.{table}")).mappings()]
            actual_hash = _rows_digest(actual_rows)
            if len(actual_rows) != len(records[table]) or actual_hash != report["row_hashes"][table]:
                raise RuntimeError("迁移后行数或校验和不一致，已回滚")
            verified_hashes[table] = actual_hash
        from .workbench_projection import sync_user
        for owner in sorted(owners):
            cx.execute(text("SELECT set_config('zhida.user_id',:u,true)"), {"u": owner})
            sync_user(cx, owner, set(IMPORT_TABLES))
        report.update(applied=True, verified_row_hashes=verified_hashes,
                      imported_records=sum(report["counts"].values()))
        cx.execute(text("INSERT INTO workbench.import_receipts(source_hash,report) VALUES(:hash,CAST(:report AS jsonb))"),
                   {"hash": report["source_hash"], "report": json.dumps(report, ensure_ascii=False)})
    return report
