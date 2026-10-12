"""Explicit offline snapshot migration, dry-run by default, never opens legacy .env.

This is a data conversion tool, not live dual-writing or a cutover mechanism.
Old sessions, model caches and browser state are deliberately not imported.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from sqlalchemy import text

from .repository import Conflict, Repository, encode

ARCHIVED_TABLES = ("resumes", "candidate_profiles", "confirmed_resume_facts", "application_queue", "conflicts")
IGNORED_TABLES = {"users", "sessions", "rag_index_state", "rag_evidence", "sqlite_sequence"}


def backup_sqlite(source: Path, destination: Path) -> dict:
    source, destination = source.resolve(strict=True), destination.absolute()
    if source == destination.resolve() or destination.exists():
        raise ValueError("备份必须是另一个尚不存在的文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve a new file without overwriting any existing database.
    with destination.open("xb"):
        pass
    destination.chmod(0o600)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as reader:
        with sqlite3.connect(str(destination)) as writer:
            reader.backup(writer)
    return {"snapshot": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def inspect_snapshot(snapshot: Path, *, unowned_user: str | None = None) -> tuple[dict, dict]:
    snapshot = snapshot.resolve(strict=True)
    if not snapshot.is_file() or snapshot.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("迁移快照须为不超过100MB的SQLite文件；更大数据应单独评估")
    if Path(str(snapshot) + "-wal").exists():
        raise ValueError("请先创建独立一致快照，不能导入仍有WAL的在线数据库")
    checksum = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    data = {}
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as cx:
        cx.row_factory = sqlite3.Row
        if cx.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite快照完整性检查未通过")
        tables = {row[0] for row in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"resumes", "candidate_profiles"}.intersection(tables):
            raise ValueError("不是支持的职达SQLite快照")
        for table in (*ARCHIVED_TABLES, "users"):
            if table not in tables:
                data[table] = []
                continue
            # Table identifiers come exclusively from the fixed allowlist above.
            rows = [dict(row) for row in cx.execute('SELECT * FROM "' + table + '" LIMIT 20001')]
            if len(rows) > 20000:
                raise ValueError("单表超过20000条，停止而不是截断导入")
            data[table] = rows
        for table in ARCHIVED_TABLES:
            for row in data[table]:
                if not row.get("id"):
                    raise ValueError("旧记录缺少ID，不能猜测映射")
                if not row.get("user_id"):
                    if not unowned_user:
                        raise ValueError("存在无归属旧资料，请用 --unowned-user 明确归属后重新预览")
                    row["user_id"] = unowned_user
                if table in {"resumes", "candidate_profiles"}:
                    profile = json.loads(row["profile_json"])
                    if not isinstance(profile, dict):
                        raise ValueError("旧简历结构无效")
    report = {"source_hash": checksum, "counts": {table: len(rows) for table, rows in data.items()},
              "unmapped_tables": sorted(tables - set(ARCHIVED_TABLES) - IGNORED_TABLES),
              "not_imported": ["login_sessions", "browser_state", "files", "old_vectors"],
              "confirmation_policy": "old_data_unconfirmed", "applied": False}
    return report, data


def import_snapshot(db, snapshot: Path, *, apply: bool = False, frozen: bool = False,
                    unowned_user: str | None = None) -> dict:
    report, data = inspect_snapshot(snapshot, unowned_user=unowned_user)
    if not apply:
        return report
    if not frozen:
        raise ValueError("导入前必须停止旧库写入并明确 --confirm-frozen-snapshot")
    # Passwords remain salted hashes; tokens/cookies never enter the new database.
    users = {row["id"]: row for row in data["users"]}
    for table in ARCHIVED_TABLES:
        for row in data[table]:
            users.setdefault(row["user_id"], {"id": row["user_id"]})
    repo = Repository(db)
    with db.transaction() as cx:
        cx.execute(text("SELECT pg_advisory_xact_lock(194823901)"))
        previous = cx.execute(text("SELECT report FROM zhida.import_receipts WHERE source_hash=:hash"),
                              {"hash": report["source_hash"]}).scalar()
        if previous:
            return {**previous, "already_imported": True}
        if cx.execute(text("SELECT EXISTS(SELECT 1 FROM zhida.users)")).scalar():
            raise Conflict("仅允许导入空的专用V2数据库；不会合并或覆盖已有用户")
        for user, account in users.items():
            password_hash = account.get("password_hash") or None
            if password_hash and not password_hash.startswith("pbkdf2_sha256$"):
                password_hash = None
            cx.execute(text("INSERT INTO zhida.users(id,email,password_hash,display_name) VALUES(:id,:email,:hash,:name)"),
                       {"id": user, "email": account.get("email"), "hash": password_hash,
                        "name": account.get("display_name", "导入用户")})
        mapped_resumes = 0
        for table in ARCHIVED_TABLES:
            for row in data[table]:
                user = row["user_id"]
                cx.execute(text("SELECT set_config('zhida.user_id',:u,true)"), {"u": user})
                mapped_id = None
                if table == "resumes":
                    profile = json.loads(row["profile_json"])
                    # Old record keys are not automatically trusted as new stable IDs.
                    for section in ("education", "projects", "internships"):
                        for item in profile.get(section, []):
                            item.pop("_entity_id", None)
                    profile["_import_provenance"] = {"kind": "legacy_snapshot", "source_hash": report["source_hash"],
                                                    "legacy_resume_id": row["id"], "confirmation": "unconfirmed"}
                    saved = repo.save_resume(user, name=row.get("label") or row.get("filename") or "导入简历",
                                             profile=profile, conn=cx)
                    mapped_id = saved["id"]
                    mapped_resumes += 1
                elif table == "candidate_profiles":
                    for key, value in json.loads(row["profile_json"]).items():
                        if value is None or value == "" or isinstance(value, (list, dict)):
                            continue
                        # Lacking explicit confirmation provenance, import as unconfirmed.
                        repo.save_fact(user, subject_id="person", semantic_key="profile." + key, scope_key="global",
                            value=value, confirmation="unconfirmed", source={"kind": "legacy_snapshot",
                            "source_hash": report["source_hash"], "source_path": f"candidate_profiles:{row['id']}:{key}"}, conn=cx)
                cx.execute(text("""INSERT INTO zhida.legacy_records
                    (source_hash,table_name,record_id,user_id,payload,mapped_id)
                    VALUES(:hash,:table,:id,:u,CAST(:payload AS jsonb),:mapped)"""),
                    {"hash": report["source_hash"], "table": table, "id": row["id"], "u": user,
                     "payload": encode(row), "mapped": mapped_id})
        report.update(applied=True, mapped_resumes=mapped_resumes, imported_users=len(users),
                      archived_records=sum(len(data[t]) for t in ARCHIVED_TABLES))
        cx.execute(text("INSERT INTO zhida.import_receipts(source_hash,report) VALUES(:hash,CAST(:report AS jsonb))"),
                   {"hash": report["source_hash"], "report": encode(report)})
    return report
