"""Transactional profile, fact and run services. All reads carry tenant scope."""
from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import Connection, text

from .db import Database
from .messaging import enqueue_job

WORKFLOW_VERSIONS = frozenset({"foundation-v1", "langgraph-d1-v1", "langgraph-d2-v1", "langgraph-d3-v1"})


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass


def new_id() -> str:
    return str(uuid4())


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def canonical_scope(scope: str) -> str:
    scope = scope.strip()
    if scope.startswith("site:"):
        host = scope[len("site:"):].strip().lower().rstrip(".")
        if not host or "/" in host or ":" in host or any(char.isspace() for char in host):
            raise ValueError("站点范围应为 site:招聘域名")
        return "site:" + host
    return scope


def validate_application_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("请使用不含账号密码的 HTTPS 招聘填写页网址")
    host = parts.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost")):
        raise ValueError("填写页不能是本机页面")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("填写页不能是内网地址")
    # No network request here. Actual Runner navigation must revalidate DNS/redirects.
    return url


def require_row(conn: Connection, sql: str, parameters: dict) -> dict:
    row = conn.execute(text(sql), parameters).mappings().first()
    if row is None:
        raise NotFound("记录不存在或无权访问")
    return dict(row)


def emit_event(conn: Connection, user_id: str, run_id: str, kind: str, payload: dict) -> int:
    seq = conn.execute(text("""UPDATE zhida.application_runs SET event_seq=event_seq+1,updated_at=now()
        WHERE id=:id AND user_id=:u RETURNING event_seq"""), {"id": run_id, "u": user_id}).scalar_one()
    conn.execute(text("""INSERT INTO zhida.run_events(run_id,user_id,seq,event_type,payload)
        VALUES(:id,:u,:seq,:kind,CAST(:payload AS jsonb))"""),
        {"id": run_id, "u": user_id, "seq": seq, "kind": kind, "payload": encode(payload)})
    return seq


class Repository:
    def __init__(self, db: Database):
        self.db = db

    def ensure_user(self, user_id: str, *, display_name: str = "") -> None:
        with self.db.transaction(user_id) as conn:
            conn.execute(text("""INSERT INTO zhida.users(id,display_name) VALUES(:id,:name)
                ON CONFLICT(id) DO NOTHING"""), {"id": user_id, "name": display_name})

    def save_resume(self, user_id: str, *, name: str, profile: dict, resume_id: str | None = None,
                    expected_revision: int | None = None, file_id: str | None = None,
                    conn: Connection | None = None) -> dict:
        if conn is None:
            with self.db.transaction(user_id) as transaction:
                return self.save_resume(user_id, name=name, profile=profile, resume_id=resume_id,
                                        expected_revision=expected_revision, file_id=file_id, conn=transaction)
        if len(encode(profile)) > 500_000:
            raise ValueError("简历结构化数据过大")
        if resume_id:
            row = require_row(conn, """SELECT r.*,v.revision_no FROM zhida.resumes r
                JOIN zhida.resume_revisions v ON v.id=r.current_revision_id AND v.user_id=r.user_id
                WHERE r.id=:id AND r.user_id=:u FOR UPDATE OF r""", {"id": resume_id, "u": user_id})
            if expected_revision != row["revision_no"]:
                raise Conflict("简历版本已变化，请刷新后修改")
            revision = row["revision_no"] + 1
        else:
            resume_id, revision = new_id(), 1
            conn.execute(text("INSERT INTO zhida.resumes(id,user_id,name) VALUES(:id,:u,:name)"),
                         {"id": resume_id, "u": user_id, "name": name})
        revision_id = new_id()
        stored = json.loads(encode(profile))
        records = []
        for field, kind in (("education", "education"), ("projects", "project"), ("internships", "internship")):
            items = stored.get(field, [])
            if not isinstance(items, list) or len(items) > 100:
                raise ValueError(f"{field} 必须是最多 100 项的列表")
            seen = set()
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"{field} 每项必须是对象")
                identity = item.get("_entity_id")
                if identity:
                    require_row(conn, """SELECT id FROM zhida.experience_entities
                        WHERE user_id=:u AND resume_id=:r AND id=:id AND entity_type=:kind""",
                        {"u": user_id, "r": resume_id, "id": identity, "kind": kind})
                else:
                    # New records receive stable IDs returned to the client for subsequent revisions.
                    identity = new_id()
                    conn.execute(text("""INSERT INTO zhida.experience_entities(id,user_id,resume_id,entity_type)
                        VALUES(:id,:u,:r,:kind)"""), {"id": identity, "u": user_id, "r": resume_id, "kind": kind})
                if identity in seen:
                    raise ValueError("同一简历中不能重复引用同一经历实体")
                seen.add(identity)
                item["_entity_id"] = identity
                records.append((identity, field, index, item))
        conn.execute(text("""INSERT INTO zhida.resume_revisions
            (id,user_id,resume_id,revision_no,file_id,profile,content_hash)
            VALUES(:id,:u,:r,:n,:file,CAST(:profile AS jsonb),:hash)"""),
            {"id": revision_id, "u": user_id, "r": resume_id, "n": revision, "file": file_id,
             "profile": encode(stored), "hash": digest(stored)})
        for identity, field, index, item in records:
            conn.execute(text("""INSERT INTO zhida.experience_versions
                (id,user_id,resume_id,entity_id,resume_revision_id,education_level,content,source_span)
                VALUES(:id,:u,:r,:entity,:rev,:degree,CAST(:content AS jsonb),:span)"""),
                {"id": new_id(), "u": user_id, "r": resume_id, "entity": identity, "rev": revision_id,
                 "degree": item.get("degree") if field == "education" else None,
                 "content": encode(item), "span": f"{field}[{index}]"})
        conn.execute(text("UPDATE zhida.resumes SET current_revision_id=:rev,name=:name WHERE id=:id AND user_id=:u"),
                     {"rev": revision_id, "name": name, "id": resume_id, "u": user_id})
        # A separate source per immutable resume revision allows intentionally pinned historical runs.
        from .rag import RagService
        evidence = []
        for identity, field, index, item in records:
            title = item.get("name") or item.get("school") or item.get("organization") or field
            body = "\n".join(f"{key}: {value if isinstance(value, str) else encode(value)}"
                             for key, value in item.items() if not key.startswith("_") and value)
            evidence.append({"entity_id": identity, "entity_type": {"education": "education", "projects": "project", "internships": "internship"}[field],
                             "title": str(title)[:120], "text": body, "source_path": f"{field}[{index}]"})
        if stored.get("skills"):
            evidence.append({"entity_id": f"skills:{resume_id}", "entity_type": "skill", "title": "技能",
                             "text": encode(stored["skills"]), "source_path": "skills"})
        source = None
        if evidence:
            if len(evidence) > 100:
                raise ValueError("一份简历最多包含 100 个独立知识实体（含技能）")
            source = RagService(self.db).put_source(user_id, records=evidence, source_kind="resume", title=name,
                        resume_revision_id=revision_id, conn=conn)
            enqueue_job(conn, user_id=user_id, kind="rag.index", aggregate_id=source["source_revision_id"],
                        idempotency_key=f"index:{source['source_revision_id']}",
                        payload={"source_revision_id": source["source_revision_id"]})
        return {"id": resume_id, "revision_id": revision_id, "revision_no": revision, "name": name,
                "profile": stored, "index_source": source}

    def list_resumes(self, user_id: str) -> list[dict]:
        with self.db.transaction(user_id) as conn:
            return [dict(row) for row in conn.execute(text("""SELECT r.id,r.name,r.current_revision_id,
                v.revision_no,v.profile FROM zhida.resumes r JOIN zhida.resume_revisions v
                ON v.id=r.current_revision_id AND v.user_id=r.user_id
                WHERE r.user_id=:u AND r.deleted_at IS NULL ORDER BY r.created_at DESC"""), {"u": user_id}).mappings()]

    def save_fact(self, user_id: str, *, subject_id: str, semantic_key: str, scope_key: str,
                  value: Any, expected_revision: int = 0, confirmation: str = "confirmed",
                  source: dict | None = None, valid_until: datetime | None = None,
                  question: str = "", conn: Connection | None = None) -> dict:
        if conn is None:
            with self.db.transaction(user_id) as transaction:
                return self.save_fact(user_id, subject_id=subject_id, semantic_key=semantic_key, scope_key=scope_key,
                    value=value, expected_revision=expected_revision, confirmation=confirmation, source=source,
                    valid_until=valid_until, question=question, conn=transaction)
        # All fact/run commands take the user lock before fact/run/plan locks. In
        # particular, an answer must not race a standalone fact correction with
        # the opposite run -> fact / fact -> run lock order.
        require_row(conn, "SELECT id FROM zhida.users WHERE id=:u FOR UPDATE", {"u": user_id})
        if not all((subject_id, semantic_key, scope_key)) or len(encode(value)) > 20000:
            raise ValueError("事实必须有主体、语义和范围，值大小不超过 20KB")
        scope_key = canonical_scope(scope_key)
        if not scope_key:
            raise ValueError("资料范围不能为空")
        identity = str(uuid5(NAMESPACE_URL, encode([user_id, subject_id, semantic_key, scope_key])))
        conn.execute(text("""INSERT INTO zhida.profile_facts(id,user_id,subject_id,semantic_key,scope_key)
            VALUES(:id,:u,:subject,:key,:scope) ON CONFLICT(id) DO NOTHING"""),
            {"id": identity, "u": user_id, "subject": subject_id, "key": semantic_key, "scope": scope_key})
        fact = require_row(conn, "SELECT * FROM zhida.profile_facts WHERE id=:id AND user_id=:u FOR UPDATE",
                           {"id": identity, "u": user_id})
        revision = 0
        if fact["current_revision_id"]:
            revision = conn.execute(text("SELECT revision_no FROM zhida.fact_revisions WHERE id=:id AND user_id=:u"),
                                    {"id": fact["current_revision_id"], "u": user_id}).scalar_one()
        if revision != expected_revision:
            raise Conflict("资料已被修改，请读取最新版本后确认")
        new_revision = new_id()
        conn.execute(text("""INSERT INTO zhida.fact_revisions
            (id,user_id,fact_id,revision_no,value,source,confirmation,valid_until)
            VALUES(:id,:u,:fact,:n,CAST(:value AS jsonb),CAST(:source AS jsonb),:confirmation,:until)"""),
            {"id": new_revision, "u": user_id, "fact": identity, "n": revision + 1, "value": encode(value),
             "source": encode(source or {"kind": "user_confirmation"}), "confirmation": confirmation, "until": valid_until})
        conn.execute(text("UPDATE zhida.profile_facts SET current_revision_id=:rev WHERE id=:id AND user_id=:u"),
                     {"rev": new_revision, "id": identity, "u": user_id})
        conn.execute(text("""UPDATE zhida.answer_memories SET fact_revision_id=:rev
            WHERE user_id=:u AND fact_id=:id"""), {"rev": new_revision, "u": user_id, "id": identity})
        if question and confirmation == "confirmed":
            conn.execute(text("""INSERT INTO zhida.answer_memories
                (id,user_id,fact_id,fact_revision_id,question_semantics,scope_key)
                VALUES(:id,:u,:fact,:revision,:q,:scope)
                ON CONFLICT(user_id,fact_id,question_semantics,scope_key) DO UPDATE SET
                fact_id=EXCLUDED.fact_id,fact_revision_id=EXCLUDED.fact_revision_id"""),
                {"id": new_id(), "u": user_id, "fact": identity, "revision": new_revision,
                 "q": question, "scope": scope_key})
        # Lock runs before plans, matching cancellation and worker effect order.
        # Keep pending confirmations paused even when a different fact changes.
        active = conn.execute(text("""UPDATE zhida.application_runs AS r SET run_revision=run_revision+1,
            status=CASE WHEN EXISTS (SELECT 1 FROM zhida.confirmation_requests q
                WHERE q.user_id=r.user_id AND q.run_id=r.id AND q.status='pending')
                THEN 'awaiting_user' ELSE r.status END,
            updated_at=now() WHERE user_id=:u AND status IN
            ('queued','running','awaiting_user','awaiting_browser','retry_wait','reconciling')
            RETURNING id,run_revision,status"""), {"u": user_id}).mappings().all()
        # Invalidate all pending plans for this user conservatively, rather than missing a dependency.
        conn.execute(text("UPDATE zhida.fill_plans SET status='invalidated' WHERE user_id=:u AND status='planned'"), {"u": user_id})
        conn.execute(text("UPDATE zhida.action_ledger SET status='invalidated' WHERE user_id=:u AND status='planned'"), {"u": user_id})
        for run in active:
            emit_event(conn, user_id, run["id"], "fact.updated", {"fact_id": identity, "revision_id": new_revision})
            pending = conn.execute(text("""SELECT 1 FROM zhida.confirmation_requests
                WHERE user_id=:u AND run_id=:r AND status='pending' LIMIT 1"""),
                {"u": user_id, "r": run["id"]}).first()
            if not pending:
                enqueue_job(conn, user_id=user_id, kind="workflow.advance", aggregate_id=run["id"],
                            expected_revision=run["run_revision"], idempotency_key=f"fact:{new_revision}:run:{run['id']}",
                            payload={"reason": "reobserve"})
        # Arbitrary identifiers/contacts need no semantic embeddings. Confirmed question meaning does.
        from .rag import RagService
        source_id = f"fact:{identity}"
        current = conn.execute(text("SELECT * FROM rag.knowledge_sources WHERE user_id=:u AND id=:id FOR UPDATE"),
                               {"u": user_id, "id": source_id}).mappings().first()
        question = question or (current["title"] if current else "")
        if confirmation != "confirmed" and current:
            RagService(self.db).revoke_source(user_id, source_id, conn=conn)
        elif question and confirmation == "confirmed":
            # Never widen an opaque/resume-specific scope to global retrieval. Unknown scopes
            # remain in exact facts only until the caller supplies an explicit supported scope.
            resume_scope, site_scope = None, None
            if scope_key.startswith("resume:"):
                resume_scope = scope_key[len("resume:"):]
                require_row(conn, "SELECT id FROM zhida.resume_revisions WHERE user_id=:u AND id=:id",
                            {"u": user_id, "id": resume_scope})
            elif scope_key.startswith("site:"):
                site_scope = scope_key[len("site:"):].lower().strip()
                if not site_scope or "/" in site_scope or ":" in site_scope:
                    raise ValueError("站点范围应为 site:招聘域名")
            elif scope_key != "global":
                return {"id": identity, "revision_id": new_revision, "revision_no": revision + 1,
                        "value": value, "confirmation": confirmation}
            if current and current["revoked_at"]:
                # Only a new explicit confirmed fact revision may revive this internal source.
                # Source/head update below is in the same transaction; no old generation resurfaces.
                conn.execute(text("UPDATE rag.knowledge_sources SET revoked_at=NULL WHERE user_id=:u AND id=:id"),
                             {"u": user_id, "id": source_id})
            source_result = RagService(self.db).put_source(user_id, source_id=source_id,
                expected_revision=current["revision_no"] if current else 0,
                records=[{"entity_id": subject_id, "entity_type": "confirmed_answer", "title": question[:120],
                          "text": f"问题：{question}\n确认答案：{encode(value)}", "source_path": f"fact:{new_revision}"}],
                source_kind="confirmed_answer", title=question, resume_revision_id=resume_scope,
                site_scope=site_scope, conn=conn)
            enqueue_job(conn, user_id=user_id, kind="rag.index", aggregate_id=source_result["source_revision_id"],
                        idempotency_key=f"index:{source_result['source_revision_id']}",
                        payload={"source_revision_id": source_result["source_revision_id"]})
        return {"id": identity, "revision_id": new_revision, "revision_no": revision + 1,
                "value": value, "confirmation": confirmation}

    def facts(self, user_id: str, *, include_unconfirmed: bool = False) -> list[dict]:
        with self.db.transaction(user_id) as conn:
            return [dict(row) for row in conn.execute(text("""SELECT f.id,f.subject_id,f.semantic_key,f.scope_key,
                r.id AS revision_id,r.revision_no,r.value,r.source,r.confirmation,r.valid_until
                FROM zhida.profile_facts f JOIN zhida.fact_revisions r
                ON r.id=f.current_revision_id AND r.user_id=f.user_id
                WHERE f.user_id=:u AND (:all OR (r.confirmation='confirmed'
                AND (r.valid_until IS NULL OR r.valid_until>now()))) ORDER BY f.semantic_key"""),
                {"u": user_id, "all": include_unconfirmed}).mappings()]

    def remembered_answer(self, user_id: str, *, question: str, scope_key: str,
                          subject_id: str, semantic_key: str) -> dict | None:
        """Exact memory lookup always rechecks the current fact; no fuzzy/global fallback."""
        with self.db.transaction(user_id) as cx:
            row = cx.execute(text("""SELECT r.id AS revision_id,r.value,f.subject_id,f.semantic_key,f.scope_key
                FROM zhida.answer_memories m JOIN zhida.profile_facts f ON f.id=m.fact_id AND f.user_id=m.user_id
                JOIN zhida.fact_revisions r ON r.id=f.current_revision_id AND r.user_id=f.user_id
                WHERE m.user_id=:u AND m.question_semantics=:q AND m.scope_key=:scope
                  AND f.subject_id=:subject AND f.semantic_key=:semantic
                  AND r.confirmation='confirmed' AND (r.valid_until IS NULL OR r.valid_until>now())"""),
                {"u": user_id, "q": question, "scope": canonical_scope(scope_key),
                 "subject": subject_id, "semantic": semantic_key}).mappings().first()
            return dict(row) if row else None

    def create_run(self, user_id: str, *, resume_revision_id: str, application_url: str,
                   idempotency_key: str, browser_session_id: str | None = None,
                   workflow_version: str = "foundation-v1") -> dict:
        if workflow_version not in WORKFLOW_VERSIONS:
            raise ValueError("不支持的工作流版本")
        url = validate_application_url(application_url)
        request_hash = digest([resume_revision_id, url, browser_session_id])
        with self.db.transaction(user_id) as conn:
            # Serialize per user; two simultaneous clicks cannot both pass the active-run check.
            require_row(conn, "SELECT id FROM zhida.users WHERE id=:u FOR UPDATE", {"u": user_id})
            prior = conn.execute(text("SELECT * FROM zhida.application_runs WHERE user_id=:u AND idempotency_key=:key"),
                                 {"u": user_id, "key": idempotency_key}).mappings().first()
            if prior:
                if prior["request_hash"] != request_hash or prior["workflow_version"] != workflow_version:
                    raise Conflict("同一幂等键不能用于不同填写请求")
                return dict(prior)
            require_row(conn, """SELECT v.id FROM zhida.resume_revisions v JOIN zhida.resumes r
                ON r.id=v.resume_id AND r.user_id=v.user_id
                WHERE v.user_id=:u AND v.id=:id AND r.deleted_at IS NULL""",
                        {"u": user_id, "id": resume_revision_id})
            if browser_session_id:
                require_row(conn, "SELECT id FROM zhida.browser_sessions WHERE user_id=:u AND id=:id",
                            {"u": user_id, "id": browser_session_id})
            if conn.execute(text("""SELECT id FROM zhida.application_runs WHERE user_id=:u AND status IN
                ('queued','running','awaiting_user','awaiting_browser','retry_wait','reconciling')"""), {"u": user_id}).first():
                raise Conflict("已有填写任务，请继续或取消原任务")
            run_id = new_id()
            conn.execute(text("""INSERT INTO zhida.application_runs
                (id,user_id,resume_revision_id,browser_session_id,application_url,idempotency_key,request_hash,workflow_version)
                VALUES(:id,:u,:rev,:session,:url,:key,:hash,:workflow)"""),
                {"id": run_id, "u": user_id, "rev": resume_revision_id, "session": browser_session_id,
                 "url": url, "key": idempotency_key, "hash": request_hash, "workflow": workflow_version})
            emit_event(conn, user_id, run_id, "run.created", {"stage": "queued", "workflow": workflow_version})
            enqueue_job(conn, user_id=user_id, kind="workflow.advance", aggregate_id=run_id,
                        expected_revision=1, idempotency_key=f"run:{run_id}:1")
            return require_row(conn, "SELECT * FROM zhida.application_runs WHERE id=:id AND user_id=:u",
                               {"id": run_id, "u": user_id})

    def get_run(self, user_id: str, run_id: str) -> dict:
        with self.db.transaction(user_id) as conn:
            row = require_row(conn, "SELECT * FROM zhida.application_runs WHERE id=:id AND user_id=:u",
                              {"id": run_id, "u": user_id})
            row["questions"] = [dict(r) for r in conn.execute(text("""SELECT * FROM zhida.confirmation_requests
                WHERE run_id=:id AND user_id=:u AND status='pending' ORDER BY id"""),
                {"id": run_id, "u": user_id}).mappings()]
            if row['workflow_version'] == 'langgraph-d2-v1':
                row['questions'] = [dict(r) for r in conn.execute(text("""SELECT q.*,f.control_type,
                    f.evidence_refs AS input_evidence,COALESCE(fr.revision_no,0) AS expected_fact_revision
                    FROM zhida.confirmation_requests q
                    JOIN zhida.questions f ON f.user_id=q.user_id AND f.id=q.question_id
                    LEFT JOIN zhida.profile_facts pf ON pf.user_id=q.user_id AND pf.subject_id=q.subject_id
                        AND pf.semantic_key=q.semantic_key AND pf.scope_key=q.scope_key
                    LEFT JOIN zhida.fact_revisions fr ON fr.user_id=pf.user_id AND fr.id=pf.current_revision_id
                    WHERE q.run_id=:id AND q.user_id=:u AND q.status='pending' ORDER BY q.id"""),
                    {'id': run_id, 'u': user_id}).mappings()]
            return row

    def events(self, user_id: str, run_id: str, after: int = 0, limit: int = 100) -> list[dict]:
        with self.db.transaction(user_id) as conn:
            require_row(conn, "SELECT id FROM zhida.application_runs WHERE id=:id AND user_id=:u", {"id": run_id, "u": user_id})
            return [dict(row) for row in conn.execute(text("""SELECT seq,event_type,payload,created_at
                FROM zhida.run_events WHERE user_id=:u AND run_id=:id AND seq>:after
                ORDER BY seq LIMIT :limit"""),
                {"u": user_id, "id": run_id, "after": max(0, after), "limit": min(max(limit, 1), 200)}).mappings()]

    def cancel_run(self, user_id: str, run_id: str, expected_revision: int) -> dict:
        with self.db.transaction(user_id) as conn:
            require_row(conn, "SELECT id FROM zhida.users WHERE id=:u FOR UPDATE", {"u": user_id})
            row = require_row(conn, "SELECT * FROM zhida.application_runs WHERE id=:id AND user_id=:u FOR UPDATE",
                              {"id": run_id, "u": user_id})
            if row["run_revision"] != expected_revision:
                raise Conflict("任务版本已变化")
            conn.execute(text("""UPDATE zhida.application_runs SET status='cancelled',
                run_revision=run_revision+1,updated_at=now() WHERE id=:id AND user_id=:u"""), {"id": run_id, "u": user_id})
            conn.execute(text("UPDATE zhida.fill_plans SET status='invalidated' WHERE run_id=:id AND user_id=:u AND status='planned'"),
                         {"id": run_id, "u": user_id})
            conn.execute(text("UPDATE zhida.action_ledger SET status='invalidated' WHERE run_id=:id AND user_id=:u AND status='planned'"),
                         {"id": run_id, "u": user_id})
            emit_event(conn, user_id, run_id, "run.cancelled", {"inflight_actions": "must_reconcile"})
            return {"id": run_id, "status": "cancelled", "run_revision": expected_revision + 1}

    def resume_run(self, user_id: str, run_id: str, expected_revision: int) -> dict:
        """Wake a paused run, never replay an action or silently clear uncertainty.

        A successful command consumes one run revision. Repeated/concurrent
        requests with that revision get 409, leaving exactly one durable wakeup.
        """
        with self.db.transaction(user_id) as conn:
            require_row(conn, "SELECT id FROM zhida.users WHERE id=:u FOR UPDATE", {"u": user_id})
            run = require_row(conn, "SELECT * FROM zhida.application_runs WHERE id=:id AND user_id=:u FOR UPDATE",
                              {"id": run_id, "u": user_id})
            if run["run_revision"] != expected_revision:
                raise Conflict("任务版本已变化，请刷新后继续")
            if run["workflow_version"] not in {"langgraph-d1-v1", "langgraph-d2-v1"}:
                raise Conflict("此任务未使用可恢复的 LangGraph 工作流，请保留原任务流程")
            if run["status"] not in {"awaiting_browser", "awaiting_user", "retry_wait", "reconciling"}:
                raise Conflict("任务正在执行或已经结束，不能重复继续")
            require_row(conn, """SELECT v.id FROM zhida.resume_revisions v JOIN zhida.resumes r
                ON r.id=v.resume_id AND r.user_id=v.user_id
                WHERE v.user_id=:u AND v.id=:id AND r.deleted_at IS NULL""",
                {"u": user_id, "id": run["resume_revision_id"]})
            if conn.execute(text("""SELECT 1 FROM zhida.confirmation_requests
                WHERE user_id=:u AND run_id=:r AND status='pending' LIMIT 1"""),
                {"u": user_id, "r": run_id}).first():
                raise Conflict("请先回答全部待确认问题；任务仍在等待你的确认")
            uncertain = bool(conn.execute(text("""SELECT 1 FROM zhida.action_ledger
                WHERE user_id=:u AND run_id=:r AND status IN ('dispatched','unknown') LIMIT 1"""),
                {"u": user_id, "r": run_id}).first())
            state, reason = ("reconciling", "reconcile") if uncertain else ("queued", "reobserve")
            conn.execute(text("""UPDATE zhida.application_runs SET status=:state,
                run_revision=run_revision+1,updated_at=now() WHERE id=:r AND user_id=:u"""),
                {"state": state, "r": run_id, "u": user_id})
            # Old executable plans never survive a user-triggered resumption.
            conn.execute(text("""UPDATE zhida.fill_plans SET status='invalidated'
                WHERE run_id=:r AND user_id=:u AND status='planned'"""), {"r": run_id, "u": user_id})
            conn.execute(text("""UPDATE zhida.action_ledger SET status='invalidated'
                WHERE run_id=:r AND user_id=:u AND status='planned'"""), {"r": run_id, "u": user_id})
            revision = expected_revision + 1
            job = enqueue_job(conn, user_id=user_id, kind="workflow.advance", aggregate_id=run_id,
                expected_revision=revision, idempotency_key=f"resume:{run_id}:{revision}", payload={"reason": reason})
            emit_event(conn, user_id, run_id, "run.resume_requested",
                       {"run_revision": revision, "reason": reason, "browser_actions_executed": 0})
            return {"id": run_id, "status": state, "run_revision": revision,
                    "job_id": job.job_id, "resume_reason": reason, "browser_actions_executed": 0}

    def answer(self, user_id: str, run_id: str, confirmation_id: str, *, value: Any,
               expected_revision: int, expected_fact_revision: int = 0,
               expected_question_revision: int | None = None) -> dict:
        with self.db.transaction(user_id) as conn:
            require_row(conn, "SELECT id FROM zhida.users WHERE id=:u FOR UPDATE", {"u": user_id})
            run = require_row(conn, "SELECT * FROM zhida.application_runs WHERE id=:id AND user_id=:u FOR UPDATE",
                              {"id": run_id, "u": user_id})
            if run["run_revision"] != expected_revision or run["status"] != "awaiting_user":
                raise Conflict("任务或补充问题已变化，请刷新")
            if run["workflow_version"] in {"langgraph-d1-v1", "langgraph-d2-v1"} and expected_question_revision is None:
                raise Conflict("请提供当前题目版本后确认")
            question = require_row(conn, """SELECT * FROM zhida.confirmation_requests
                WHERE id=:id AND user_id=:u AND run_id=:r AND status='pending' FOR UPDATE""",
                {"id": confirmation_id, "u": user_id, "r": run_id})
            if expected_question_revision is not None and question["question_revision"] != expected_question_revision:
                raise Conflict("问题内容已变化，请读取最新题目后确认")
            if run['workflow_version'] == 'langgraph-d2-v1':
                from .answer_planning import validate_confirmation_answer
                spec = require_row(conn, 'SELECT control_type,evidence_refs FROM zhida.questions WHERE user_id=:u AND id=:q',
                                   {'u': user_id, 'q': question['question_id']})
                evidence = spec['evidence_refs']
                if not isinstance(evidence, dict) or evidence.get('schema_version') != 'd2-question-evidence-v1':
                    raise Conflict('题目输入方式尚未确认，请重新采集页面')
                value = validate_confirmation_answer(spec['control_type'], question['options'], value,
                                                     date_precision=evidence.get('date_precision'))
            elif question["options"] and value not in question["options"]:
                raise ValueError("答案必须来自本题真实选项")
            result = self.save_fact(user_id, subject_id=question["subject_id"], semantic_key=question["semantic_key"],
                scope_key=question["scope_key"], value=value, expected_revision=expected_fact_revision,
                question=question["label"], source={"kind": "user_confirmation", "confirmation_id": confirmation_id}, conn=conn)
            conn.execute(text("""UPDATE zhida.confirmation_requests SET status='answered',answer_fact_revision_id=:fact
                WHERE id=:id AND user_id=:u"""), {"fact": result["revision_id"], "id": confirmation_id, "u": user_id})
            pending = conn.execute(text("""SELECT count(*) FROM zhida.confirmation_requests
                WHERE run_id=:r AND user_id=:u AND status='pending'"""), {"r": run_id, "u": user_id}).scalar_one()
            revision = expected_revision + 1
            if not pending:
                conn.execute(text("UPDATE zhida.application_runs SET status='queued' WHERE id=:r AND user_id=:u"), {"r": run_id, "u": user_id})
                enqueue_job(conn, user_id=user_id, kind="workflow.advance", aggregate_id=run_id,
                    expected_revision=revision, idempotency_key=f"answers:{run_id}:{revision}",
                    payload={"reason": "reobserve"})
            emit_event(conn, user_id, run_id, "question.answered",
                       {"confirmation_id": confirmation_id, "pending_questions": pending, "run_revision": revision})
            return {**result, "run_revision": revision, "status": "awaiting_user" if pending else "queued",
                    "pending_questions": pending}
