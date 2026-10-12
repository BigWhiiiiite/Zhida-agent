"""Versioned, owner-scoped PostgreSQL hybrid retrieval.

SQL performs ANN/lexical candidate search and batch evidence hydration. It never
loads a user's entire vector corpus into Python. Fake embedders are supported
for contract tests; they cannot establish real model retrieval quality.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re
from typing import Any, Optional
import uuid

from sqlalchemy import text

from .embedding import Embedder, EmbeddingUnavailable, LocalBGEEmbedder, MAX_INPUT_TOKENS, vector_literal
from .redis_control import CacheBusy, private_key

CHUNKER_VERSION = "fact-boundaries-token-bounded-v1"
TOKENIZER_VERSION = "zh-bigram-tech-v1"
RETRIEVER_VERSION = "hnsw20-gin20-rrf60-0604-v1"
MAX_EXACT_CANDIDATES = 5000


class SourceConflict(ValueError):
    """A concurrent edit or changed source scope requires a fresh read."""


def _id() -> str:
    return str(uuid.uuid4())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def lexical_tokens(value: str) -> list[str]:
    """Deterministic Chinese character/bigram segmentation, not native BM25.

    Stable technical aliases distinguish the React framework from ReAct agent
    reasoning. Lexical expansion never modifies the source quotation.
    """
    result: list[str] = []
    for match in re.finditer(r"[\u3400-\u9fff]+|[A-Za-z0-9]+(?:[+#._-][A-Za-z0-9+#]+)*", value):
        token = match.group()
        if "\u3400" <= token[0] <= "\u9fff":
            result.extend(token)
            result.extend(token[i:i + 2] for i in range(len(token) - 1))
        elif token == "ReAct":
            result.append("agentreactpattern")
        elif token == "React":
            result.append("frontendreactframework")
        else:
            result.append(re.sub(r"[^a-z0-9]", "", token.lower()))
    return [token for token in result if token]


def lexical_query(value: str) -> str:
    # Tokens contain only letters, digits and CJK: no tsquery operators enter.
    return " | ".join(dict.fromkeys(lexical_tokens(value)))


def reciprocal_rank_fusion(dense: list[str], lexical: list[str], *, limit: int = 10) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranked, weight in ((dense, 0.6), (lexical, 0.4)):
        for rank, item_id in enumerate(dict.fromkeys(ranked), start=1):
            scores[item_id] = scores.get(item_id, 0) + weight / (60 + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]


def split_records(records: list[dict], embedder: Embedder) -> list[dict]:
    """Split facts with exact source offsets and token-aware long-clause handling."""
    chunks: list[dict] = []
    for record_index, record in enumerate(records):
        content = record["text"]
        context = f"{record['entity_type']}：{record['title']}。"
        if embedder.token_count(context) >= MAX_INPUT_TOKENS - 16:
            raise ValueError("Parent title exceeds embedding context budget")
        for match in re.finditer(r"[^\n\r。！？；;]+[。！？；;]?", content):
            start, end = match.span()
            while start < end and content[start].isspace():
                start += 1
            while end > start and content[end - 1].isspace():
                end -= 1
            while start < end:
                finish = end
                if embedder.token_count(context + content[start:finish]) > MAX_INPUT_TOKENS:
                    low, high = start + 1, finish
                    while low < high:
                        middle = (low + high + 1) // 2
                        if embedder.token_count(context + content[start:middle]) <= MAX_INPUT_TOKENS:
                            low = middle
                        else:
                            high = middle - 1
                    finish = low
                    if embedder.token_count(context + content[start:finish]) > MAX_INPUT_TOKENS:
                        raise ValueError("Unable to fit source fragment into embedding budget")
                quote = content[start:finish]
                chunks.append({
                    "entity_id": record["entity_id"], "entity_type": record["entity_type"],
                    "text": quote, "embedding_text": context + quote,
                    "parent_context": context, "source_path": record["source_path"],
                    "source_span": {"record": record_index, "start": start, "end": finish},
                    "text_hash": _hash(quote), "lexical_text": " ".join(lexical_tokens(context + quote)),
                })
                start = finish
    return chunks


class RagService:
    def __init__(self, db, embedder: Optional[Embedder] = None, *, query_cache=None,
                 query_cache_prefix: str = "zhida:v2:cache:qvec"):
        self.db = db
        self.embedder = embedder if embedder is not None else LocalBGEEmbedder()
        self.query_cache = query_cache
        self.query_cache_prefix = query_cache_prefix

    def _query_vector(self, user_id: str, query: str, profile_id: str) -> str:
        try:
            return self._cached_query_vector(user_id, query, profile_id)
        except CacheBusy:
            raise CacheBusy("Query embedding cache is busy") from None
        except Exception:
            # A plugin/cache exception may include its input. Never propagate
            # private query text or cached vector values into HTTP/log errors.
            raise EmbeddingUnavailable("Query embedding is temporarily unavailable") from None

    def _cached_query_vector(self, user_id: str, query: str, profile_id: str) -> str:
        if self.query_cache is None:
            return vector_literal(self.embedder.embed_query(query))
        key = private_key(self.query_cache_prefix, user_id=user_id,
                          embedding_profile=profile_id, query_hash=_hash(query))
        values = self.query_cache.get_or_load(
            key, lambda: self.embedder.embed_query(query), ttl_seconds=3600)
        try:
            return vector_literal(values)
        except (ValueError, TypeError):
            # A malformed cache entry is not a valid model response. Remove it
            # and make one coordinated reload; never cache evidence/answers.
            self.query_cache.invalidate(key)
            values = self.query_cache.get_or_load(
                key, lambda: self.embedder.embed_query(query), ttl_seconds=3600)
            return vector_literal(values)

    @contextmanager
    def _transaction(self, user_id: str, connection=None):
        if not user_id:
            raise ValueError("An explicit user scope is required")
        if connection is not None:
            yield connection
        else:
            with self.db.transaction(user_id=user_id) as conn:
                yield conn

    def put_source(self, user_id: str, *, records: list[dict], source_kind: str = "resume",
                   title: str = "", resume_revision_id: Optional[str] = None,
                   site_scope: Optional[str] = None, source_id: Optional[str] = None,
                   expected_revision: Optional[int] = None, conn=None) -> dict:
        """Create/revise immutable source data. Caller enqueues in this transaction."""
        if source_kind not in {"resume", "confirmed_answer", "site_rule", "profile", "user_note"}:
            raise ValueError("Unsupported private knowledge source kind")
        if not isinstance(records, list) or not 1 <= len(records) <= 100:
            raise ValueError("Source must have 1 to 100 records")
        cleaned = []
        entity_ids = set()
        for item in records:
            item = dict(item)
            entity_id = str(item.get("entity_id", "")).strip()
            entity_type = str(item.get("entity_type", "")).strip()
            content = str(item.get("text", ""))
            if not entity_id or entity_id in entity_ids or not entity_type or not content.strip():
                raise ValueError("Records require distinct stable entity IDs, type and non-empty text")
            if len(content) > 200000 or len(str(item.get("title", ""))) > 120:
                raise ValueError("Source record exceeds content or title limit")
            entity_ids.add(entity_id)
            cleaned.append({"entity_id": entity_id, "entity_type": entity_type,
                            "title": str(item.get("title", "")), "text": content,
                            "source_path": str(item.get("source_path", entity_id))})
        if sum(len(row["text"]) for row in cleaned) > 1000000:
            raise ValueError("Source is too large")
        payload = _json(cleaned)
        with self._transaction(user_id, conn) as cx:
            if source_id is None or expected_revision == 0:
                if expected_revision not in (None, 0):
                    raise SourceConflict("New source must start at revision 0")
                source_id = source_id or _id()
                cx.execute(text("""INSERT INTO rag.knowledge_sources
                    (id,user_id,source_kind,title,resume_revision_id,site_scope)
                    VALUES (:id,:user,:kind,:title,:resume,:site)
                    ON CONFLICT(id) DO NOTHING"""),
                    {"id": source_id, "user": user_id, "kind": source_kind, "title": title,
                     "resume": resume_revision_id, "site": site_scope})
            source = cx.execute(text("""SELECT * FROM rag.knowledge_sources
                WHERE user_id=:user AND id=:id FOR UPDATE"""), {"user": user_id, "id": source_id}).mappings().first()
            if source is None or source["revoked_at"] is not None:
                raise LookupError("Source not found")
            if source["revision_no"] > 0 and expected_revision is None:
                raise SourceConflict("Updating a source requires its expected_revision")
            if expected_revision is not None and expected_revision != source["revision_no"]:
                raise SourceConflict("Source has changed")
            if (source["source_kind"], source["resume_revision_id"], source["site_scope"]) != (
                    source_kind, resume_revision_id, site_scope):
                raise SourceConflict("Source scope is immutable; create a separate source")
            revision_no = source["revision_no"] + 1
            revision_id = _id()
            cx.execute(text("""INSERT INTO rag.source_revisions
                (id,user_id,source_id,revision_no,content_hash,records)
                VALUES (:id,:user,:source,:revision,:hash,CAST(:records AS jsonb))"""),
                {"id": revision_id, "user": user_id, "source": source_id, "revision": revision_no,
                 "hash": _hash(payload), "records": payload})
            cx.execute(text("""UPDATE rag.knowledge_sources SET current_revision_id=:revision,
                revision_no=:number,title=:title WHERE id=:source AND user_id=:user"""),
                {"revision": revision_id, "number": revision_no, "title": title,
                 "source": source_id, "user": user_id})
            # Clear pointers in the same update: heads can never point to a
            # generation of a different desired revision (also enforced by FK).
            cx.execute(text("""UPDATE rag.active_index_heads SET desired_source_revision_id=:revision,
                lexical_generation_id=NULL,vector_generation_id=NULL,revision=revision+1
                WHERE source_id=:source AND user_id=:user"""),
                {"revision": revision_id, "source": source_id, "user": user_id})
            return {"source_id": source_id, "source_revision_id": revision_id, "revision": revision_no}

    def revoke_source(self, user_id: str, source_id: str, *, conn=None) -> dict:
        with self._transaction(user_id, conn) as cx:
            row = cx.execute(text("""UPDATE rag.knowledge_sources SET revoked_at=COALESCE(revoked_at,now())
                WHERE id=:source AND user_id=:user RETURNING id"""),
                {"source": source_id, "user": user_id}).first()
            if row is None:
                raise LookupError("Source not found")
            cx.execute(text("""UPDATE rag.active_index_heads SET lexical_generation_id=NULL,
                vector_generation_id=NULL,revision=revision+1 WHERE source_id=:source AND user_id=:user"""),
                {"source": source_id, "user": user_id})
        return {"source_id": source_id, "status": "revoked"}

    def _register_profile(self, cx) -> dict:
        profile = self.embedder.profile
        if profile["dimension"] != 512 or profile["metric"] != "cosine":
            raise ValueError("V1 vector storage requires 512-dimensional cosine embeddings")
        cx.execute(text("""INSERT INTO rag.embedding_profiles
            (id,model_name,model_revision,dimension,metric,configuration)
            VALUES (:id,:model_name,:model_revision,:dimension,:metric,CAST(:configuration AS jsonb))
            ON CONFLICT(id) DO NOTHING"""), {**profile, "configuration": _json(profile["configuration"])})
        stored = cx.execute(text("SELECT * FROM rag.embedding_profiles WHERE id=:id"),
                            {"id": profile["id"]}).mappings().one()
        if any(stored[key] != profile[key] for key in (
                "model_name", "model_revision", "dimension", "metric", "configuration")):
            raise SourceConflict("Embedding profile identity was reused with different configuration")
        return profile

    def index_source(self, user_id: str, source_revision_id: str) -> dict:
        """Build with compare-and-swap activation; late old jobs never win."""
        with self._transaction(user_id) as cx:
            source = cx.execute(text("""SELECT r.*,s.current_revision_id,s.revoked_at AS source_revoked
                FROM rag.source_revisions r JOIN rag.knowledge_sources s
                ON s.id=r.source_id AND s.user_id=r.user_id
                WHERE r.id=:id AND r.user_id=:user"""),
                {"id": source_revision_id, "user": user_id}).mappings().first()
            if source is None:
                raise LookupError("Source revision not found")
            if source["source_revoked"] or source["revoked_at"] or source["current_revision_id"] != source_revision_id:
                return {"status": "obsolete", "activated": False, "chunk_count": 0}
            profile = self._register_profile(cx)
            generation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _json([
                user_id, source_revision_id, profile["id"], CHUNKER_VERSION, TOKENIZER_VERSION])))
            cx.execute(text("""INSERT INTO rag.index_generations
                (id,user_id,source_id,source_revision_id,embedding_profile_id,chunker_version,tokenizer_version,state)
                VALUES (:id,:user,:source,:revision,:profile,:chunker,:tokenizer,'building')
                ON CONFLICT(user_id,source_revision_id,embedding_profile_id,chunker_version,tokenizer_version)
                DO NOTHING"""), {"id": generation_id, "user": user_id, "source": source["source_id"],
                    "revision": source_revision_id, "profile": profile["id"],
                    "chunker": CHUNKER_VERSION, "tokenizer": TOKENIZER_VERSION})
            generation = cx.execute(text("""SELECT * FROM rag.index_generations
                WHERE user_id=:user AND id=:id FOR UPDATE"""),
                {"user": user_id, "id": generation_id}).mappings().one()
            if generation["state"] == "ready":
                activated = self._activate(cx, user_id, source, generation_id, profile["id"], vector=True)
                return {"generation_id": generation_id, "status": "ready", "activated": activated,
                        "chunk_count": generation["ready_count"]}
            build_token = _id()
            cx.execute(text("""UPDATE rag.index_generations SET build_token=:token,state='building'
                WHERE id=:id AND user_id=:user"""),
                {"token": build_token, "id": generation_id, "user": user_id})
            records = source["records"]

        chunks = split_records(records, self.embedder)
        if not chunks:
            raise ValueError("No source facts were produced")
        for ordinal, chunk in enumerate(chunks):
            chunk.update(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{generation_id}:{ordinal}")), ordinal=ordinal)
        with self._transaction(user_id) as cx:
            if not self._owns_build(cx, user_id, generation_id, build_token):
                return {"generation_id": generation_id, "status": "superseded", "activated": False}
            rows = [{**chunk, "user": user_id, "generation": generation_id, "source_revision": source_revision_id,
                     "source_span": _json(chunk["source_span"])} for chunk in chunks]
            cx.execute(text("""INSERT INTO rag.evidence_chunks
                (id,user_id,generation_id,source_revision_id,entity_id,entity_type,ordinal,text,embedding_text,
                 parent_context,source_path,source_span,text_hash,lexemes)
                VALUES (:id,:user,:generation,:source_revision,:entity_id,:entity_type,:ordinal,:text,:embedding_text,
                 :parent_context,:source_path,CAST(:source_span AS jsonb),:text_hash,to_tsvector('simple',:lexical_text))
                ON CONFLICT(generation_id,ordinal) DO NOTHING"""), rows)
            cx.execute(text("""UPDATE rag.index_generations SET state='lexical_ready',expected_count=:count
                WHERE user_id=:user AND id=:id"""), {"count": len(chunks), "user": user_id, "id": generation_id})
            self._activate(cx, user_id, source, generation_id, profile["id"], vector=False)
        try:
            vectors = self.embedder.embed_documents([chunk["embedding_text"] for chunk in chunks])
            if len(vectors) != len(chunks):
                raise ValueError("Embedding model returned a different number of vectors")
            encoded = [vector_literal(vector) for vector in vectors]
        except Exception:
            with self._transaction(user_id) as cx:
                cx.execute(text("""UPDATE rag.index_generations SET state='failed'
                    WHERE id=:id AND user_id=:user AND build_token=:token"""),
                    {"id": generation_id, "user": user_id, "token": build_token})
            raise
        with self._transaction(user_id) as cx:
            if not self._owns_build(cx, user_id, generation_id, build_token):
                return {"generation_id": generation_id, "status": "superseded", "activated": False}
            cx.execute(text("""INSERT INTO rag.chunk_vectors_bge512_v1
                (chunk_id,user_id,generation_id,source_revision_id,entity_id,embedding)
                VALUES (:id,:user,:generation,:source_revision,:entity_id,CAST(:vector AS vector))
                ON CONFLICT(chunk_id) DO NOTHING"""),
                [{"id": chunk["id"], "user": user_id, "generation": generation_id,
                  "source_revision": source_revision_id, "entity_id": chunk["entity_id"], "vector": vector}
                 for chunk, vector in zip(chunks, encoded)])
            count = cx.execute(text("""SELECT count(*) FROM rag.chunk_vectors_bge512_v1
                WHERE user_id=:user AND generation_id=:id"""), {"user": user_id, "id": generation_id}).scalar_one()
            if count != len(chunks):
                raise ValueError("Vector count does not match generation manifest")
            cx.execute(text("""UPDATE rag.index_generations SET state='ready',ready_count=:count
                WHERE user_id=:user AND id=:id"""), {"count": count, "user": user_id, "id": generation_id})
            activated = self._activate(cx, user_id, source, generation_id, profile["id"], vector=True)
            if not activated:
                cx.execute(text("UPDATE rag.index_generations SET state='obsolete' WHERE id=:id AND user_id=:user"),
                           {"id": generation_id, "user": user_id})
        return {"generation_id": generation_id, "status": "ready" if activated else "obsolete",
                "activated": activated, "chunk_count": count}

    @staticmethod
    def _owns_build(cx, user_id, generation_id, token) -> bool:
        return cx.execute(text("""SELECT build_token=:token FROM rag.index_generations
            WHERE user_id=:user AND id=:id FOR UPDATE"""),
            {"token": token, "user": user_id, "id": generation_id}).scalar_one() is True

    @staticmethod
    def _activate(cx, user_id, source, generation_id, profile_id, *, vector) -> bool:
        # Source row is locked so concurrent correction/revocation either
        # precedes this check or invalidates these heads in its own transaction.
        valid = cx.execute(text("""SELECT id FROM rag.knowledge_sources
            WHERE user_id=:user AND id=:source AND current_revision_id=:revision AND revoked_at IS NULL
            FOR UPDATE"""), {"user": user_id, "source": source["source_id"], "revision": source["id"]}).first()
        if not valid:
            return False
        cx.execute(text("""INSERT INTO rag.active_index_heads
            (user_id,source_id,retrieval_profile,desired_source_revision_id,lexical_generation_id,vector_generation_id)
            VALUES (:user,:source,:profile,:revision,:generation,:vector)
            ON CONFLICT(user_id,source_id,retrieval_profile) DO UPDATE SET
              lexical_generation_id=EXCLUDED.lexical_generation_id,
              vector_generation_id=CASE WHEN :vector IS NOT NULL THEN EXCLUDED.vector_generation_id
                                       ELSE rag.active_index_heads.vector_generation_id END,
              revision=rag.active_index_heads.revision+1
            WHERE rag.active_index_heads.desired_source_revision_id=EXCLUDED.desired_source_revision_id"""),
            {"user": user_id, "source": source["source_id"], "profile": profile_id,
             "revision": source["id"], "generation": generation_id, "vector": generation_id if vector else None})
        return True

    def search(self, user_id: str, query: str, *, resume_revision_id: Optional[str] = None,
               source_ids: Optional[list[str]] = None, entity_ids: Optional[list[str]] = None,
               site_scope: Optional[str] = None, limit: int = 5) -> dict:
        if not query.strip() or len(query) > 6000 or not 1 <= limit <= 10:
            raise ValueError("Query or result limit is invalid")
        if source_ids == [] or entity_ids == []:
            return {"mode": "empty_scope", "evidence": [], "generations": []}
        profile_id = self.embedder.profile["id"]
        base = {"user": user_id, "profile": profile_id,
                "fact_site": site_scope, "fact_resume": resume_revision_id}
        clauses = ["s.user_id=:user", "s.revoked_at IS NULL", "r.revoked_at IS NULL",
                   "h.retrieval_profile=:profile", "h.desired_source_revision_id=s.current_revision_id",
                   """(s.source_kind<>'confirmed_answer' OR EXISTS (
                       SELECT 1 FROM zhida.profile_facts f JOIN zhida.fact_revisions fr
                       ON fr.user_id=f.user_id AND fr.id=f.current_revision_id
                       WHERE f.user_id=s.user_id AND s.id='fact:'||f.id
                       AND fr.confirmation='confirmed' AND (fr.valid_until IS NULL OR fr.valid_until>now())
                       AND (f.scope_key='global' OR f.scope_key='site:'||CAST(:fact_site AS text)
                            OR f.scope_key='resume:'||CAST(:fact_resume AS text))
                   ))"""]
        if resume_revision_id is None:
            # Unscoped search must not silently mix all of a user's resumes.
            if source_ids is None:
                clauses.append("s.resume_revision_id IS NULL")
        else:
            clauses.append("(s.resume_revision_id=:resume OR s.resume_revision_id IS NULL)")
            base["resume"] = resume_revision_id
        if source_ids is not None:
            clauses.append("s.id=ANY(CAST(:sources AS text[]))")
            base["sources"] = source_ids
        if site_scope is None:
            clauses.append("s.site_scope IS NULL")
        else:
            clauses.append("(s.site_scope=:site OR s.site_scope IS NULL)")
            base["site"] = site_scope
        with self._transaction(user_id) as cx:
            heads = list(cx.execute(text("""SELECT h.*,s.current_revision_id FROM rag.active_index_heads h
                JOIN rag.knowledge_sources s ON s.id=h.source_id AND s.user_id=h.user_id
                JOIN rag.source_revisions r ON r.id=s.current_revision_id AND r.user_id=s.user_id
                WHERE """ + " AND ".join(clauses)), base).mappings())
        lexical_generations = [h["lexical_generation_id"] for h in heads if h["lexical_generation_id"]]
        vector_generations = [h["vector_generation_id"] for h in heads if h["vector_generation_id"]]
        if not lexical_generations and not vector_generations:
            return {"mode": "not_indexed", "evidence": [], "generations": []}
        query_vector = None
        degraded_reason = None
        if vector_generations:
            try:
                query_vector = self._query_vector(user_id, query, profile_id)
            except (EmbeddingUnavailable, ValueError, CacheBusy) as exc:
                degraded_reason = type(exc).__name__
        dense: list[str] = []
        dense_raw: list[str] = []
        lexical: list[str] = []
        mode = "hybrid_degraded" if query_vector is None else "ann"
        entity_filter = " AND c.entity_id=ANY(CAST(:entities AS text[]))" if entity_ids else ""
        params = {"user": user_id, "entities": entity_ids, "vector": query_vector,
                  "vector_generations": vector_generations, "lexical_generations": lexical_generations,
                  "query": lexical_query(query), "take": 20}
        with self._transaction(user_id) as cx:
            if query_vector is not None:
                cx.execute(text("SET LOCAL hnsw.ef_search=100"))
                cx.execute(text("SET LOCAL hnsw.iterative_scan='strict_order'"))
                cx.execute(text("SET LOCAL hnsw.max_scan_tuples=20000"))
                dense_sql = """SELECT c.chunk_id AS id FROM rag.chunk_vectors_bge512_v1 c
                    WHERE c.user_id=:user AND c.generation_id=ANY(CAST(:vector_generations AS text[]))""" + entity_filter
                dense = list(cx.execute(text(dense_sql +
                    " ORDER BY c.embedding <=> CAST(:vector AS vector) LIMIT :take"), params).scalars())
                dense_raw = list(dense)
                if len(dense) < 20:
                    # Bounded count, then a MATERIALIZED scope prevents ANN
                    # pruning during exact fallback. Nothing goes to Python.
                    count = cx.execute(text("SELECT count(*) FROM (" + dense_sql +
                        f" LIMIT {MAX_EXACT_CANDIDATES + 1}) bounded"), params).scalar_one()
                    if len(dense) < min(20, count) and count <= MAX_EXACT_CANDIDATES:
                        exact_sql = """WITH scope AS MATERIALIZED (
                            SELECT c.chunk_id AS id,c.embedding FROM rag.chunk_vectors_bge512_v1 c
                            WHERE c.user_id=:user AND c.generation_id=ANY(CAST(:vector_generations AS text[]))""" + entity_filter + """
                            ) SELECT id FROM scope ORDER BY embedding <=> CAST(:vector AS vector) LIMIT :take"""
                        dense = list(cx.execute(text(exact_sql), params).scalars())
                        mode = "exact_filtered"
            if lexical_generations and params["query"]:
                lexical = list(cx.execute(text("""SELECT c.id FROM rag.evidence_chunks c
                    WHERE c.user_id=:user AND c.generation_id=ANY(CAST(:lexical_generations AS text[]))
                    AND c.lexemes @@ to_tsquery('simple',:query)""" + entity_filter + """
                    ORDER BY ts_rank_cd(c.lexemes,to_tsquery('simple',:query)) DESC,c.id LIMIT :take"""), params).scalars())
            ranked = reciprocal_rank_fusion(dense, lexical)
            hydrated = {}
            if ranked:
                # Final read checks current source validity again. A correction
                # between candidate search and hydration excludes old evidence.
                rows = cx.execute(text("""SELECT c.id AS chunk_id,c.generation_id,c.entity_id,c.entity_type,
                    c.text,c.parent_context,c.source_path,c.source_span,c.source_revision_id,s.id AS source_id,
                    s.source_kind,s.resume_revision_id,s.site_scope
                    FROM rag.evidence_chunks c
                    JOIN rag.source_revisions r ON r.id=c.source_revision_id AND r.user_id=c.user_id
                    JOIN rag.knowledge_sources s ON s.id=r.source_id AND s.user_id=r.user_id
                    WHERE c.user_id=:user AND c.id=ANY(CAST(:ids AS text[]))
                    AND s.current_revision_id=r.id AND s.revoked_at IS NULL AND r.revoked_at IS NULL
                    AND (s.source_kind<>'confirmed_answer' OR EXISTS (
                      SELECT 1 FROM zhida.profile_facts f JOIN zhida.fact_revisions fr
                      ON fr.user_id=f.user_id AND fr.id=f.current_revision_id
                      WHERE f.user_id=s.user_id AND s.id='fact:'||f.id
                      AND c.source_path='fact:'||fr.id AND fr.confirmation='confirmed'
                      AND (fr.valid_until IS NULL OR fr.valid_until>now())
                      AND (f.scope_key='global' OR f.scope_key='site:'||CAST(:fact_site AS text)
                           OR f.scope_key='resume:'||CAST(:fact_resume AS text))
                    ))"""),
                    {"user": user_id, "ids": [item_id for item_id, _ in ranked],
                     "fact_site": site_scope, "fact_resume": resume_revision_id}).mappings()
                hydrated = {row["chunk_id"]: dict(row) for row in rows}
        evidence = []
        entity_counts: dict[str, int] = {}
        for item_id, score in ranked:
            row = hydrated.get(item_id)
            if row is None:
                continue
            if entity_counts.get(row["entity_id"], 0) >= 2:
                continue
            entity_counts[row["entity_id"]] = entity_counts.get(row["entity_id"], 0) + 1
            # Retrieval provenance is not authority to fill a website. A user
            # note/site rule/profile can contain unverified text or instructions.
            trust = {"confirmed_answer": "confirmed_fact", "resume": "selected_resume_evidence"}.get(
                row["source_kind"], "unverified_source")
            provenance = {key: row[key] for key in (
                "source_kind", "source_id", "source_revision_id", "source_path", "generation_id")}
            evidence.append({**row, "score": score, "trust": trust, "provenance": provenance,
                             "auto_fill_allowed": False})
            if len(evidence) >= limit:
                break
        return {"mode": mode, "evidence": evidence,
                "generations": sorted(set(lexical_generations + vector_generations)),
                "retriever_version": RETRIEVER_VERSION,
                "candidate_ids": {"dense_raw": dense_raw, "dense": dense, "lexical": lexical},
                "degraded_reason": degraded_reason}
