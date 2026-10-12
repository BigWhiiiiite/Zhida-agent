"""Project the PostgreSQL workbench's single write source into versioned RAG.

Called inside its transaction, never writes SQLite or embeds inline. The V2
gateway disables native business writes until the UI has adopted that contract.
Original confirmations and complete JSON remain unchanged in workbench tables.
"""
from __future__ import annotations

import copy
import json
from collections import Counter
from uuid import uuid4

from sqlalchemy import text

from .messaging import enqueue_job
from .rag import RagService
from .repository import Repository, digest, encode


def _anchor(section: str, item: dict) -> tuple:
    fields = {"education": ("school", "degree"), "projects": ("name",),
              "internships": ("organization", "role")}[section]
    return tuple(str(item.get(k, "")).strip().casefold() for k in fields)


def preserve_entities(profile: dict, previous: dict) -> dict:
    """Never match experiences by array position; ambiguity gets a new identity."""
    result = copy.deepcopy(profile)
    for section in ("education", "projects", "internships"):
        old = previous.get(section, [])
        fresh = result.get(section, [])
        old_counts = Counter(_anchor(section, x) for x in old)
        new_counts = Counter(_anchor(section, x) for x in fresh)
        lookup = {_anchor(section, x): x for x in old}
        for item in fresh:
            item.pop("_entity_id", None)
            key = _anchor(section, item)
            previous_item = lookup.get(key, {})
            old_start, new_start = previous_item.get("start_date"), item.get("start_date")
            different_period = bool(old_start and new_start and old_start != new_start)
            if (any(key) and old_counts[key] == new_counts[key] == 1
                    and previous_item.get("_entity_id") and not different_period):
                item["_entity_id"] = previous_item["_entity_id"]
    return result


def _save_map(cx, user: str, kind: str, old_id: str, core_id: str, fingerprint: str, payload: dict):
    cx.execute(text("""INSERT INTO workbench.projections
        (user_id,kind,legacy_id,core_id,source_hash,payload)
        VALUES(:u,:kind,:old,:core,:hash,CAST(:payload AS jsonb))
        ON CONFLICT(user_id,kind,legacy_id) DO UPDATE SET core_id=excluded.core_id,
        source_hash=excluded.source_hash,payload=excluded.payload,deleted_at=NULL"""),
        {"u": user, "kind": kind, "old": old_id, "core": core_id,
         "hash": fingerprint, "payload": encode(payload)})


def _source_records(profile: dict, prefix: str) -> list[dict]:
    records = []
    for section, kind in (("education", "education"), ("projects", "project"), ("internships", "internship")):
        for index, item in enumerate(profile.get(section, [])):
            identity = item["_entity_id"]
            title = item.get("name") or item.get("school") or item.get("organization") or section
            # This source is evidence, never an instruction or permission to fill.
            body = "\n".join(f"{key}: {value if isinstance(value, str) else encode(value)}"
                for key, value in item.items() if value and not key.startswith("_"))
            if body:
                records.append({"entity_id": identity, "entity_type": kind, "title": str(title)[:120],
                    "text": body, "source_path": f"{section}[{index}]"})
    for field in ("skills", "languages", "certificates", "awards"):
        if profile.get(field):
            records.append({"entity_id": f"{prefix}:{field}", "entity_type": "skill", "title": field,
                "text": encode(profile[field]), "source_path": field})
    return records


def _sync_resume(cx, user, row, mapping):
    profile = json.loads(row["profile_json"])
    fingerprint = digest([row["label"], profile])
    if mapping and mapping["source_hash"] == fingerprint and not mapping["deleted_at"]:
        return
    previous = mapping["payload"].get("profile", {}) if mapping else {}
    profile = preserve_entities(profile, previous)
    profile["_workbench_origin"] = row["id"]
    args = {}
    if mapping:
        revision = cx.execute(text("""SELECT v.revision_no FROM zhida.resumes r
            JOIN zhida.resume_revisions v ON v.id=r.current_revision_id AND v.user_id=r.user_id
            WHERE r.id=:id AND r.user_id=:u"""), {"id": mapping["core_id"], "u": user}).scalar_one()
        args = {"resume_id": mapping["core_id"], "expected_revision": revision}
        cx.execute(text("UPDATE zhida.resumes SET deleted_at=NULL WHERE id=:id AND user_id=:u"),
                   {"id": mapping["core_id"], "u": user})
    saved = Repository(None).save_resume(user, name=row["label"] or row["filename"], profile=profile, conn=cx, **args)
    _save_map(cx, user, "resume", row["id"], saved["id"], fingerprint,
              {"profile": saved["profile"], "revision_id": saved["revision_id"]})


def _sync_profile(cx, user, row, mapping):
    profile = json.loads(row["profile_json"])
    _sync_answer_facts(cx,user,profile)
    fingerprint = digest(profile)
    if mapping and mapping["source_hash"] == fingerprint and not mapping["deleted_at"]:
        return
    source_id = "workbench:profile:" + user
    previous = mapping["payload"].get("profile", {}) if mapping else {}
    profile = preserve_entities(profile, previous)
    for section in ("education", "projects", "internships"):
        for item in profile.get(section, []):
            item.setdefault("_entity_id", str(uuid4()))
    records = _source_records(profile, source_id)
    current = cx.execute(text("SELECT * FROM rag.knowledge_sources WHERE id=:id AND user_id=:u FOR UPDATE"),
                         {"id": source_id, "u": user}).mappings().first()
    if records:
        if current and current["revoked_at"]:
            cx.execute(text("UPDATE rag.knowledge_sources SET revoked_at=NULL WHERE id=:id AND user_id=:u"),
                       {"id": source_id, "u": user})
        source = RagService(None).put_source(user, source_id=source_id,
            expected_revision=current["revision_no"] if current else 0, records=records,
            source_kind="profile", title="主档案经历证据", conn=cx)
        enqueue_job(cx, user_id=user, kind="rag.index", aggregate_id=source["source_revision_id"],
            idempotency_key=f"index:{source['source_revision_id']}", payload={"source_revision_id": source["source_revision_id"]})
    elif current and not current["revoked_at"]:
        RagService(None).revoke_source(user, source_id, conn=cx)
    # Contacts, identity numbers and sensitive personal facts do not need vectorization.
    # Keep original confirmation scopes exactly; no promotion to global facts here.
    _save_map(cx, user, "profile", row["id"], source_id, fingerprint,
              {"profile": {key: profile.get(key, []) for key in ("education", "projects", "internships")}})


def sync_user(cx, user_id: str, changed_tables: set[str]) -> None:
    if not user_id or not {"resumes", "candidate_profiles", "application_knowledge"}.intersection(changed_tables):
        return
    cx.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": "projection:" + user_id})
    cx.execute(text("INSERT INTO zhida.users(id) VALUES(:u) ON CONFLICT(id) DO NOTHING"), {"u": user_id})
    if 'application_knowledge' in changed_tables:
        _sync_knowledge(cx,user_id)
    for table, kind, handler in (("resumes", "resume", _sync_resume), ("candidate_profiles", "profile", _sync_profile)):
        if table not in changed_tables:
            continue
        previous = {r["legacy_id"]: dict(r) for r in cx.execute(text("""SELECT * FROM workbench.projections
            WHERE user_id=:u AND kind=:kind FOR UPDATE"""), {"u": user_id, "kind": kind}).mappings()}
        rows = cx.execute(text(f"SELECT * FROM workbench.{table} WHERE user_id=:u"), {"u": user_id}).mappings().all()
        ids = {row["id"] for row in rows}
        for row in rows:
            handler(cx, user_id, row, previous.get(row["id"]))
        for old_id, record in previous.items():
            if old_id in ids or record["deleted_at"]:
                continue
            if kind == "resume":
                cx.execute(text("UPDATE zhida.resumes SET deleted_at=now() WHERE id=:id AND user_id=:u"),
                           {"id": record["core_id"], "u": user_id})
                sources = cx.execute(text("""SELECT s.id FROM rag.knowledge_sources s
                    JOIN zhida.resume_revisions r ON r.id=s.resume_revision_id AND r.user_id=s.user_id
                    WHERE s.user_id=:u AND r.resume_id=:id AND s.revoked_at IS NULL"""),
                    {"u": user_id, "id": record["core_id"]}).scalars().all()
            else:
                sources = cx.execute(text("""SELECT id FROM rag.knowledge_sources
                    WHERE id=:id AND user_id=:u AND revoked_at IS NULL"""),
                    {"id": record["core_id"], "u": user_id}).scalars().all()
            for source_id in sources:
                RagService(None).revoke_source(user_id, source_id, conn=cx)
            cx.execute(text("""UPDATE workbench.projections SET deleted_at=now()
                WHERE user_id=:u AND kind=:kind AND legacy_id=:id"""), {"u": user_id, "kind": kind, "id": old_id})


def _sync_answer_facts(cx,user,profile):
    """Only explicit answer memories, never arbitrary imported profile fields.

    Preserve ALL old matching restrictions in an opaque compound scope. These
    are authoritative exact facts, not global answers. D3 still revalidates the
    original question/options/entity through the workbench mapper.
    """
    memories=profile.get('application_answer_memory',[])
    seen=set()
    grouped={}
    for item in memories:
        if not item.get('id') or not str(item.get('value','')).strip(): continue
        scope='workbench:'+digest([item.get(k,'') for k in
            ('resume_id','company_scope','source_host','entity_scope','normalized_question','option_fingerprint')])
        subject=item.get('entity_scope') or 'application'
        key=item.get('semantic_key') or 'application.custom'
        grouped.setdefault((scope,subject,key),[]).append(item)
    for (scope,subject,key),items in grouped.items():
        item=items[0]
        conflicting=len({str(i['value']) for i in items})>1
        confirmation='unconfirmed' if conflicting else 'confirmed'
        value=None if conflicting else item['value']
        old=cx.execute(text("""SELECT f.id,r.revision_no,r.value,r.confirmation FROM zhida.profile_facts f
            JOIN zhida.fact_revisions r ON r.id=f.current_revision_id AND r.user_id=f.user_id
            WHERE f.user_id=:u AND f.subject_id=:subject AND f.semantic_key=:key AND f.scope_key=:scope"""),
            {'u':user,'subject':subject,'key':key,'scope':scope}).mappings().first()
        if old and old['value']==value and old['confirmation']==confirmation:
            seen.add(old['id']);continue
        saved=Repository(None).save_fact(user,subject_id=subject,semantic_key=key,scope_key=scope,
            value=value,confirmation=confirmation,expected_revision=old['revision_no'] if old else 0,question=item.get('question',''),
            source={'kind':'workbench_user_confirmation','memory_id':item['id']},conn=cx)
        seen.add(saved['id'])
    # Removing memory must revoke its fact, not leave a second truth source.
    rows=cx.execute(text("""SELECT f.id,r.revision_no,f.subject_id,f.semantic_key,f.scope_key
        FROM zhida.profile_facts f JOIN zhida.fact_revisions r ON r.id=f.current_revision_id AND r.user_id=f.user_id
        WHERE f.user_id=:u AND r.source->>'kind'='workbench_user_confirmation' AND r.confirmation='confirmed'"""),
        {'u':user}).mappings().all()
    for old in rows:
        if old['id'] not in seen:
            Repository(None).save_fact(user,subject_id=old['subject_id'],semantic_key=old['semantic_key'],
                scope_key=old['scope_key'],value=None,confirmation='revoked',expected_revision=old['revision_no'],
                source={'kind':'workbench_user_confirmation'},conn=cx)


def _sync_knowledge(cx,user):
    """Index company mapping/rule text in shards, not Python vector scans."""
    rows=cx.execute(text('SELECT id,site_scope,embedding_text FROM workbench.application_knowledge '
                         'WHERE user_id=:u ORDER BY site_scope,id'),{'u':user}).mappings().all()
    grouped={}
    for row in rows: grouped.setdefault(row['site_scope'],[]).append(row)
    prefix='workbench:knowledge:'+user+':'
    old={r['id']:dict(r) for r in cx.execute(text('SELECT * FROM rag.knowledge_sources WHERE user_id=:u '
        "AND left(id,length(:prefix))=:prefix"),{'u':user,'prefix':prefix}).mappings()}
    seen=set()
    for scope,items in grouped.items():
        for offset in range(0,len(items),100):
            identity=prefix+digest(scope)+':'+str(offset//100);seen.add(identity)
            source=old.get(identity)
            records=[{'entity_id':r['id'],'entity_type':'site_rule','title':'本人确认的字段对照/公司规则',
                'text':r['embedding_text'],'source_path':'application_knowledge:'+r['id']} for r in items[offset:offset+100]]
            if source and not source['revoked_at']:
                previous=cx.execute(text('SELECT records FROM rag.source_revisions WHERE user_id=:u AND id=:id'),
                    {'u':user,'id':source['current_revision_id']}).scalar()
                if previous==records:continue
            if source and source['revoked_at']:
                cx.execute(text('UPDATE rag.knowledge_sources SET revoked_at=NULL WHERE user_id=:u AND id=:id'),
                    {'u':user,'id':identity})
            # Unchanged shards above emit no new generation. Outbox
            # idempotency uses the actual source revision, not an array index.
            result=RagService(None).put_source(user,source_id=identity,source_kind='site_rule',site_scope=scope,
                records=records,title='已确认招聘题目映射',expected_revision=source['revision_no'] if source else 0,conn=cx)
            enqueue_job(cx,user_id=user,kind='rag.index',aggregate_id=result['source_revision_id'],
                idempotency_key='index:'+result['source_revision_id'],payload={'source_revision_id':result['source_revision_id']})
    for identity,source in old.items():
        if identity not in seen and not source['revoked_at']:
            RagService(None).revoke_source(user,identity,conn=cx)
