"""Existing recommendation response contract backed by indexed V2 retrieval."""
from functools import lru_cache

from .db import Database
from .rag import RagService
from .redis_control import RedisControl, VersionedCache
from .settings import Settings


@lru_cache(maxsize=1)
def service():
    from redis import Redis
    cfg = Settings.from_env()
    db = Database(cfg.database_url, pool_size=cfg.pool_size)
    cache = None
    if cfg.redis_cache_url and cfg.redis_control_url:
        control = RedisControl(Redis.from_url(cfg.redis_control_url, socket_timeout=2, socket_connect_timeout=2),
                               prefix=f"zhida:v2:{cfg.environment}")
        cache = VersionedCache(Redis.from_url(cfg.redis_cache_url, socket_timeout=2, socket_connect_timeout=2), control)
    return RagService(db, query_cache=cache)


def knowledge_candidates(user,scope,queries):
    from sqlalchemy import text
    from .repository import digest
    rag=service()
    prefix='workbench:knowledge:'+user+':'+digest(scope)+':'
    with rag.db.transaction(user) as cx:
        sources=list(cx.execute(text("SELECT id FROM rag.knowledge_sources WHERE user_id=:u "
            "AND left(id,length(:prefix))=:prefix AND revoked_at IS NULL"),{'u':user,'prefix':prefix}).scalars())
    result=[]
    # Bound sequential query embedding/SQL work. Exact confirmed mappings are
    # checked separately for every field, even beyond this candidate budget.
    for position,query in enumerate(queries):
        if position>=16:
            result.append({'ids':set(),'mode':'budget_limited'});continue
        if not sources:
            result.append({'ids':set(),'mode':'not_indexed'});continue
        try:
            found=rag.search(user,query,source_ids=sources,site_scope=scope,limit=5)
            result.append({'ids':{e['entity_id'] for e in found['evidence']},'mode':found['mode']})
        except Exception:
            # Exact confirmed mapping still works during an index outage.
            result.append({'ids':set(),'mode':'unavailable'})
    return result


def enrich(batch, profile, aliases):
    from .. import storage
    from ..job_models import JobEvidenceMatch
    from ..job_rag import MAX_ENRICHED_JOBS, EvidenceChunk, _requirements, _lexical_score, _evidence_for_job
    user = storage.current_user_id()
    target = batch.jobs[:MAX_ENRICHED_JOBS]
    requirements = list(dict.fromkeys(pair for item in target for pair in _requirements(item.job)))
    lookup, modes, evidence_ids = {}, set(), set()
    retrieved = {}
    for requirement, kind in requirements:
        if requirement not in retrieved:
            retrieved[requirement] = service().search(user, requirement,
                source_ids=["workbench:profile:" + user], limit=5)
        response = retrieved[requirement]
        modes.add(response["mode"])
        lookup[(requirement, kind)] = None
        for row in response["evidence"]:
            evidence_ids.add(row["chunk_id"])
            if row["entity_type"] not in {"project", "internship"}:
                continue
            card = EvidenceChunk(row["chunk_id"], row["entity_type"], row["parent_context"],
                row["source_path"], row["text"], row["text"], .9)
            lex = _lexical_score(requirement, card, aliases, None)
            # RRF is a rank, not cosine similarity or confidence. Do not invent a score.
            support = "direct" if lex >= .5 else "related"
            candidate = JobEvidenceMatch(requirement=requirement, requirement_type=kind,
                evidence_id=row["chunk_id"], source_kind=row["entity_type"],
                source_title=row["parent_context"], source_path=row["source_path"], quote=row["text"],
                support=support, lexical_score=round(lex, 3), semantic_score=0)
            previous = lookup[(requirement, kind)]
            if previous is None or (support == "direct" and previous.support != "direct"):
                lookup[(requirement, kind)] = candidate
    enriched = [_evidence_for_job(item, lookup) for item in target]
    tail = [_evidence_for_job(item, {}) for item in batch.jobs[MAX_ENRICHED_JOBS:]]
    jobs = sorted([*enriched, *tail], key=lambda item: (item.formal_queue_eligible, item.match_score,
        item.job.source_status == "verified"), reverse=True)
    ready = bool(modes.intersection({"ann", "exact_filtered"}))
    return batch.model_copy(update={"engine": "evidence-rag-postgres-v2", "jobs": jobs,
        "rag_status": "ready" if ready else "keyword_only" if evidence_ids else "no_evidence",
        "rag_model": "BAAI/bge-small-zh-v1.5" if ready else "", "rag_evidence_count": len(evidence_ids),
        "rag_enriched_jobs": len(enriched), "rag_message":
        "PostgreSQL 向量/全文索引检索；语义相关项只是待核对候选，不计入已掌握技能，也不代表自动填写授权"
        if evidence_ids else "主档案证据尚未建立索引或没有匹配内容，请检查索引任务状态"})
