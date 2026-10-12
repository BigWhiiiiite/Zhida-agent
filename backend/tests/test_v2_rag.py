"""Synthetic RAG contracts. Fake vectors do NOT establish model acceptance.

PostgreSQL tests are opt-in and only accept an isolated zhida_test* database.
Real-model tests additionally require ZHIDA_TEST_REAL_EMBEDDING=1; they encode
only the public synthetic strings below using the pre-installed local model.
"""
from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from sqlalchemy import text

from app.backend_v2.embedding import (EmbeddingUnavailable, LocalBGEEmbedder,
                                       MODEL_REVISION, normalized_vector)
from app.backend_v2.rag import (RagService, SourceConflict, lexical_tokens,
                                reciprocal_rank_fusion, split_records)


class FakeEmbedder:
    """Deterministic test vectors only; never used by runtime default."""
    profile = {"id": "test-only-vectors-512-v1", "model_name": "TEST_FAKE_NOT_BGE",
               "model_revision": "synthetic-v1", "dimension": 512, "metric": "cosine",
               "configuration": {"test_only": True}}

    def token_count(self, value):
        return len(value) + 2

    def embed_documents(self, values):
        return [self.embed_query(value) for value in values]

    def embed_query(self, value):
        vector = [0.0] * 512
        for token in lexical_tokens(value):
            vector[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % 512] += 1
        return normalized_vector(vector)


def record(entity="project-agent", content="负责智能体工具调用；实现检索增强生成。", kind="project"):
    return {"entity_id": entity, "entity_type": kind, "title": "合成经历 " + entity,
            "text": content, "source_path": "synthetic/" + entity}


def test_tokenizer_keeps_agent_react_distinct_from_framework():
    assert lexical_tokens("React") != lexical_tokens("ReAct")
    assert "智能" in lexical_tokens("智能体开发")


def test_rrf_uses_ranks_deduplicates_and_does_not_assume_score_probability():
    result = reciprocal_rank_fusion(["a", "a", "b"], ["b", "c"])
    assert result[0][0] == "b"
    assert len(result) == 3
    assert sum(score for _, score in result) < 1


def test_chunking_keeps_entities_source_spans_and_measured_token_limit():
    inputs = [record("master", "硕士学院人工智能；" + "参与可靠检索系统开发" * 120, "education"),
              record("bachelor", "本科学院软件工程。", "education")]
    embedder = FakeEmbedder()
    chunks = split_records(inputs, embedder)
    assert len(chunks) > 4
    for chunk in chunks:
        span = chunk["source_span"]
        original = inputs[span["record"]]
        assert original["text"][span["start"]:span["end"]] == chunk["text"]
        assert original["entity_id"] == chunk["entity_id"]
        assert embedder.token_count(chunk["embedding_text"]) <= 480


@pytest.mark.parametrize("values", [[0.0] * 512, [1.0] * 384, [float("nan")] * 512,
                                    [float("inf")] * 512])
def test_bad_vectors_rejected_before_pg(values):
    with pytest.raises(ValueError):
        normalized_vector(values)


def test_query_vector_cache_is_tenant_scoped_and_repairs_malformed_values():
    class MemoryCache:
        def __init__(self):
            self.values, self.loads = {}, 0
        def get_or_load(self, key, loader, **kwargs):
            if key not in self.values:
                self.loads += 1
                self.values[key] = loader()
            return self.values[key]
        def invalidate(self, key):
            self.values.pop(key, None)
    cache = MemoryCache()
    service = RagService(None, FakeEmbedder(), query_cache=cache)
    first = service._query_vector("user-a", "智能体开发", "profile-a")
    assert first == service._query_vector("user-a", "智能体开发", "profile-a")
    assert cache.loads == 1
    service._query_vector("user-b", "智能体开发", "profile-a")
    assert cache.loads == 2
    assert all("智能体开发" not in key for key in cache.values)
    key = next(iter(cache.values))
    cache.values[key] = [0] * 512
    assert service._query_vector("user-a", "智能体开发", "profile-a") == first
    assert cache.loads == 3


def test_query_embedding_errors_do_not_echo_private_query_or_cache_values():
    import traceback
    private = "PRIVATE_SYNTHETIC_QUERY_DO_NOT_LOG"
    embedder = FakeEmbedder()
    def broken(value):
        raise RuntimeError(value)
    embedder.embed_query = broken
    service = RagService(None, embedder)
    with pytest.raises(EmbeddingUnavailable) as raised:
        service._query_vector("synthetic-user", private, "test-profile")
    assert private not in "".join(traceback.format_exception(raised.value))
    with pytest.raises(ValueError) as raised:
        normalized_vector([private] * 512)
    assert private not in "".join(traceback.format_exception(raised.value))
    class BrokenCache:
        def get_or_load(self, key, loader, **kwargs):
            raise RuntimeError(private)
    service = RagService(None, FakeEmbedder(), query_cache=BrokenCache())
    with pytest.raises(EmbeddingUnavailable) as raised:
        service._query_vector("synthetic-user", private, "test-profile")
    assert private not in "".join(traceback.format_exception(raised.value))


def test_local_model_uses_deployment_pin_not_mutable_cache_main(tmp_path):
    root = tmp_path / "models--Qdrant--bge-small-zh-v1.5"
    (root / "snapshots" / MODEL_REVISION).mkdir(parents=True)
    (root / "refs").mkdir()
    (root / "refs" / "main").write_text("a" * 40)
    embedder = LocalBGEEmbedder(tmp_path)
    assert embedder.profile["model_revision"] == MODEL_REVISION
    profile = embedder.profile
    profile["configuration"]["query_prefix"] = "mutated"
    assert embedder.profile == LocalBGEEmbedder(tmp_path).profile
    assert embedder.profile["configuration"]["query_prefix"] != "mutated"
    with pytest.raises(EmbeddingUnavailable, match="固定版本"):
        LocalBGEEmbedder(tmp_path / "absent-cache").profile


def test_local_model_cold_load_is_atomic_across_threads(tmp_path, monkeypatch):
    """Fake loader lifecycle only, not an embedding acceptance test."""
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event
    from types import SimpleNamespace
    root = tmp_path / "models--Qdrant--bge-small-zh-v1.5" / "snapshots" / MODEL_REVISION
    root.mkdir(parents=True)
    embedder = LocalBGEEmbedder(tmp_path)
    started, proceed = Event(), Event()
    calls = []
    class FakeTokenizer:
        def no_truncation(self): pass
        def no_padding(self): pass
        @staticmethod
        def from_str(value):
            assert embedder._model is None  # no partially published model
            return FakeTokenizer()
    class FakeModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            started.set()
            assert proceed.wait(5)
            self.model = SimpleNamespace(tokenizer=SimpleNamespace(to_str=lambda: "fake"))
    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=FakeModel))
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=FakeTokenizer))
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(disable_telemetry_events=lambda: None))
    barrier = Barrier(8)
    def first_request():
        barrier.wait(5)
        return embedder._load(), embedder._tokenizer
    with ThreadPoolExecutor(max_workers=8) as pool:
        requests = [pool.submit(first_request) for _ in range(8)]
        assert started.wait(5)
        proceed.set()
        results = [request.result(timeout=5) for request in requests]
    assert len(calls) == 1
    assert calls[0]["local_files_only"] is True
    assert calls[0]["specific_model_path"] == str(root)
    assert all(model is results[0][0] and tokenizer is results[0][1] for model, tokenizer in results)
    assert results[0][1] is not None


@pytest.fixture(scope="module")
def rag_db(v2_db):
    # Shared fixture enforces the isolated database name and applies Alembic.
    return v2_db


@pytest.fixture
def owner(rag_db):
    user_id = "rag-test-" + str(uuid.uuid4())
    with rag_db.transaction(user_id) as cx:
        cx.execute(text("INSERT INTO zhida.users(id) VALUES (:id)"), {"id": user_id})
    return user_id


def add_resume(db, owner, label):
    resume_id, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db.transaction(owner) as cx:
        cx.execute(text("INSERT INTO zhida.resumes(id,user_id,name) VALUES (:id,:user,:name)"),
                   {"id": resume_id, "user": owner, "name": label})
        cx.execute(text("""INSERT INTO zhida.resume_revisions
            (id,user_id,resume_id,revision_no,profile,content_hash) VALUES (:id,:user,:resume,1,'{}','synthetic')"""),
                   {"id": revision_id, "user": owner, "resume": resume_id})
    return revision_id


def test_pg_index_hydration_version_isolation_and_revoke(rag_db, owner):
    service = RagService(rag_db, FakeEmbedder())
    resume_a, resume_b = add_resume(rag_db, owner, "Agent"), add_resume(rag_db, owner, "Backend")
    a = service.put_source(owner, records=[record()], resume_revision_id=resume_a)
    b = service.put_source(owner, records=[record("project-backend", "负责关系数据库开发与索引优化。")],
                           resume_revision_id=resume_b)
    assert service.index_source(owner, a["source_revision_id"])["activated"]
    assert service.index_source(owner, b["source_revision_id"])["activated"]
    found = service.search(owner, "智能体工具调用", resume_revision_id=resume_a)
    assert found["evidence"]
    assert all(row["source_id"] == a["source_id"] for row in found["evidence"])
    assert all(row["trust"] == "selected_resume_evidence" and not row["auto_fill_allowed"]
               for row in found["evidence"])
    assert service.search(owner, "智能体")["evidence"] == []  # no implicit cross-CV search
    with pytest.raises(SourceConflict):
        service.put_source(owner, source_id=a["source_id"], records=[record()], resume_revision_id=resume_a)
    revised = service.put_source(owner, source_id=a["source_id"], expected_revision=1,
        records=[record(content="实现最新的模型评测平台。")], resume_revision_id=resume_a)
    assert service.search(owner, "智能体", resume_revision_id=resume_a)["evidence"] == []
    assert service.index_source(owner, a["source_revision_id"])["status"] == "obsolete"
    assert service.index_source(owner, revised["source_revision_id"])["activated"]
    assert service.search(owner, "最新模型评测", resume_revision_id=resume_a)["evidence"]
    service.revoke_source(owner, a["source_id"])
    assert service.search(owner, "最新模型评测", resume_revision_id=resume_a)["evidence"] == []


@pytest.mark.parametrize("kind", ["user_note", "site_rule", "profile"])
def test_pg_user_supplied_knowledge_remains_unverified_and_never_authorizes_fill(rag_db, owner, kind):
    service = RagService(rag_db, FakeEmbedder())
    source = service.put_source(owner, source_kind=kind, records=[record()])
    indexed = service.index_source(owner, source["source_revision_id"])
    evidence = service.search(owner, "智能体工具调用")["evidence"]
    assert evidence
    for row in evidence:
        assert row["trust"] == "unverified_source"
        assert row["auto_fill_allowed"] is False
        assert row["provenance"] == {
            "source_kind": kind, "source_id": source["source_id"],
            "source_revision_id": source["source_revision_id"],
            "source_path": "synthetic/project-agent", "generation_id": indexed["generation_id"]}


def test_pg_confirmed_kind_without_current_confirmed_fact_is_not_evidence(rag_db, owner):
    service = RagService(rag_db, FakeEmbedder())
    source = service.put_source(owner, source_kind="confirmed_answer", records=[record()])
    service.index_source(owner, source["source_revision_id"])
    assert service.search(owner, "智能体工具调用")["evidence"] == []


def test_pg_other_owner_and_site_scope_cannot_leak(rag_db, owner):
    service = RagService(rag_db, FakeEmbedder())
    source = service.put_source(owner, records=[record()], site_scope="synthetic.example")
    service.index_source(owner, source["source_revision_id"])
    assert service.search(owner, "智能体")["evidence"] == []
    assert service.search(owner, "智能体", site_scope="synthetic.example")["evidence"]
    stranger = "rag-stranger-" + str(uuid.uuid4())
    with rag_db.transaction(stranger) as cx:
        cx.execute(text("INSERT INTO zhida.users(id) VALUES (:id)"), {"id": stranger})
    assert service.search(stranger, "智能体", source_ids=[source["source_id"]],
                          site_scope="synthetic.example")["evidence"] == []
    with pytest.raises(LookupError):
        service.index_source(stranger, source["source_revision_id"])
    with pytest.raises(LookupError):
        service.revoke_source(stranger, source["source_id"])


def test_pg_rls_without_application_owner_predicate(rag_db, owner):
    service = RagService(rag_db, FakeEmbedder())
    source = service.put_source(owner, records=[record()])
    # Dedicated unprivileged role exists only inside the disposable test cluster.
    with rag_db.transaction(owner) as cx:
        cx.execute(text("""DO $$ BEGIN
            IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='zhida_rag_test_reader') THEN
              CREATE ROLE zhida_rag_test_reader NOLOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
        END $$"""))
        cx.execute(text("GRANT USAGE ON SCHEMA rag TO zhida_rag_test_reader"))
        cx.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA rag TO zhida_rag_test_reader"))
        cx.execute(text("SET LOCAL ROLE zhida_rag_test_reader"))
        rows = list(cx.execute(text("SELECT id FROM rag.knowledge_sources")).scalars())
        assert rows == [source["source_id"]]
        cx.execute(text("SELECT set_config('zhida.user_id','',true)"))
        assert list(cx.execute(text("SELECT id FROM rag.knowledge_sources")).scalars()) == []


def test_pg_late_embedder_cannot_activate_old_source(rag_db, owner):
    embedder = FakeEmbedder()
    service = RagService(rag_db, embedder)
    source = service.put_source(owner, records=[record()])
    original = embedder.embed_documents
    newer = []
    def revise_during_model_call(values):
        newer.append(service.put_source(owner, source_id=source["source_id"], expected_revision=1,
                                        records=[record(content="新的已经确认的云平台经历。")]))
        return original(values)
    embedder.embed_documents = revise_during_model_call
    result = service.index_source(owner, source["source_revision_id"])
    assert result["status"] == "obsolete" and not result["activated"]
    assert service.search(owner, "智能体")["evidence"] == []
    embedder.embed_documents = original
    assert service.index_source(owner, newer[0]["source_revision_id"])["activated"]


def test_pg_lexical_result_survives_vector_failure(rag_db, owner):
    embedder = FakeEmbedder()
    service = RagService(rag_db, embedder)
    source = service.put_source(owner, records=[record()])
    def broken(values):
        raise RuntimeError("synthetic local model failure")
    embedder.embed_documents = broken
    with pytest.raises(RuntimeError):
        service.index_source(owner, source["source_revision_id"])
    result = service.search(owner, "工具调用")
    assert result["mode"] == "hybrid_degraded" and result["evidence"]
    assert result["candidate_ids"]["dense"] == []


def test_pg_source_change_during_query_is_rechecked_at_batch_hydration(rag_db, owner):
    embedder = FakeEmbedder()
    service = RagService(rag_db, embedder)
    source = service.put_source(owner, records=[record()])
    service.index_source(owner, source["source_revision_id"])
    original = embedder.embed_query
    def revise_during_query(value):
        service.put_source(owner, source_id=source["source_id"], expected_revision=1,
                           records=[record(content="更正为数据库备份工作。")])
        return original(value)
    embedder.embed_query = revise_during_query
    assert service.search(owner, "智能体工具调用")["evidence"] == []


def test_pg_confirmed_answer_checks_current_fact_and_expiry_without_reindex(rag_db, owner):
    service = RagService(rag_db, FakeEmbedder())
    fact_id, revision_id = str(uuid.uuid4()), str(uuid.uuid4())
    with rag_db.transaction(owner) as cx:
        cx.execute(text("""INSERT INTO zhida.profile_facts
            (id,user_id,subject_id,semantic_key,scope_key) VALUES (:id,:user,'self','availability','global')"""),
            {"id": fact_id, "user": owner})
        cx.execute(text("""INSERT INTO zhida.fact_revisions
            (id,user_id,fact_id,revision_no,value,source,confirmation,valid_until)
            VALUES (:id,:user,:fact,1,'"可以提前实习"','{}','confirmed',now()+interval '1 day')"""),
            {"id": revision_id, "user": owner, "fact": fact_id})
        cx.execute(text("UPDATE zhida.profile_facts SET current_revision_id=:revision WHERE id=:id AND user_id=:user"),
            {"revision": revision_id, "id": fact_id, "user": owner})
    item = record("fact-availability", "我可以提前参加实习。", "fact")
    item["source_path"] = "fact:" + revision_id
    source = service.put_source(owner, source_id="fact:" + fact_id, expected_revision=0,
                                source_kind="confirmed_answer", records=[item])
    service.index_source(owner, source["source_revision_id"])
    evidence = service.search(owner, "提前实习")["evidence"]
    assert evidence
    assert all(row["trust"] == "confirmed_fact" and row["auto_fill_allowed"] is False for row in evidence)
    with rag_db.transaction(owner) as cx:
        cx.execute(text("UPDATE zhida.profile_facts SET scope_key='site:synthetic.example' WHERE id=:id AND user_id=:user"),
                   {"id": fact_id, "user": owner})
    assert service.search(owner, "提前实习")["evidence"] == []
    assert service.search(owner, "提前实习", site_scope="synthetic.example")["evidence"]
    resume_revision = add_resume(rag_db, owner, "fact-scope")
    with rag_db.transaction(owner) as cx:
        cx.execute(text("UPDATE zhida.profile_facts SET scope_key=:scope WHERE id=:id AND user_id=:user"),
                   {"id": fact_id, "user": owner, "scope": "resume:" + resume_revision})
    assert service.search(owner, "提前实习")["evidence"] == []
    assert service.search(owner, "提前实习", resume_revision_id=resume_revision)["evidence"]
    with rag_db.transaction(owner) as cx:
        cx.execute(text("UPDATE zhida.profile_facts SET scope_key='global' WHERE id=:id AND user_id=:user"),
                   {"id": fact_id, "user": owner})
    with rag_db.transaction(owner) as cx:
        # Controlled fixture clock transition: no source/index update occurs.
        cx.execute(text("UPDATE zhida.fact_revisions SET valid_until=now()-interval '1 day' WHERE id=:id AND user_id=:user"),
                   {"id": revision_id, "user": owner})
    assert service.search(owner, "提前实习")["evidence"] == []
    new_revision = str(uuid.uuid4())
    with rag_db.transaction(owner) as cx:
        cx.execute(text("""INSERT INTO zhida.fact_revisions
            (id,user_id,fact_id,revision_no,value,source,confirmation)
            VALUES (:id,:user,:fact,2,'"目前不能实习"','{}','confirmed')"""),
                   {"id": new_revision, "user": owner, "fact": fact_id})
        cx.execute(text("UPDATE zhida.profile_facts SET current_revision_id=:revision WHERE id=:id AND user_id=:user"),
                   {"revision": new_revision, "id": fact_id, "user": owner})
    assert service.search(owner, "提前实习")["evidence"] == []


def test_pg_both_physical_indices_exist_and_can_be_planned(rag_db, owner):
    service = RagService(rag_db, FakeEmbedder())
    source = service.put_source(owner, records=[record()])
    indexed = service.index_source(owner, source["source_revision_id"])
    from app.backend_v2.embedding import vector_literal
    with rag_db.transaction(owner) as cx:
        names = set(cx.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname='rag'")).scalars())
        assert {"rag_vectors_hnsw", "rag_chunks_lexemes_gin"} <= names
        # A small fixture naturally chooses seqscan. Disabling it here only
        # proves index eligibility; it is not a production latency/plan claim.
        cx.execute(text("SET LOCAL enable_seqscan=off"))
        plan = cx.execute(text("""EXPLAIN (FORMAT JSON) SELECT chunk_id FROM rag.chunk_vectors_bge512_v1
            ORDER BY embedding <=> CAST(:vector AS vector) LIMIT 20"""),
            {"vector": vector_literal(FakeEmbedder().embed_query("智能体"))}).scalar_one()
        assert "rag_vectors_hnsw" in str(plan)
        lexical_plan = cx.execute(text("""EXPLAIN (FORMAT JSON) SELECT id FROM rag.evidence_chunks
            WHERE lexemes @@ to_tsquery('simple','智能')""")).scalar_one()
        assert "rag_chunks_lexemes_gin" in str(lexical_plan)
        assert indexed["chunk_count"] > 0


@pytest.mark.skipif(os.environ.get("ZHIDA_TEST_REAL_EMBEDDING") != "1",
                    reason="Real cached BGE verification is explicitly opt-in")
def test_real_local_bge512_and_pg_retrieval(rag_db, owner):
    embedder = LocalBGEEmbedder()
    docs = ["设计智能体工具调用与检索增强生成系统", "负责校园篮球比赛组织和体育活动"]
    vectors = embedder.embed_documents(docs)
    assert all(len(vector) == 512 for vector in vectors)
    assert all(abs(sum(value * value for value in vector) - 1) < 1e-5 for vector in vectors)
    assert vectors[0] != vectors[1]
    service = RagService(rag_db, embedder)
    source = service.put_source(owner, records=[record("agent", docs[0]), record("sport", docs[1])])
    result = service.index_source(owner, source["source_revision_id"])
    assert result["activated"]
    answer = service.search(owner, "开发能够调用工具并检索知识的AI智能体")
    assert answer["evidence"][0]["entity_id"] == "agent"
    assert answer["candidate_ids"]["dense_raw"]
    assert answer["degraded_reason"] is None
    print("Real local model snapshot:", embedder.profile["model_revision"])


@pytest.mark.skipif(os.environ.get("ZHIDA_TEST_REAL_EMBEDDING") != "1",
                    reason="Real local vector recall evaluation is opt-in")
def test_real_ann_recall20_against_scoped_exact_database_baseline(rag_db, owner):
    """Actual HNSW vs exact, before fallback/RRF; synthetic corpus, not ATS quality."""
    import json
    from app.backend_v2.embedding import vector_literal
    embedder = LocalBGEEmbedder()
    service = RagService(rag_db, embedder)
    topics = [
        ("智能体", "负责ReAct智能体规划与工具调用，增加失败恢复和多轮任务状态管理"),
        ("检索", "负责RAG知识库文档切分，使用向量索引与关键词融合检索并评估召回率"),
        ("缓存", "实现Redis缓存、请求限流与分布式锁，处理缓存穿透和热点击穿"),
        ("数据库", "负责PostgreSQL关系数据建模、SQL索引与事务一致性及故障恢复"),
        ("消息", "使用RabbitMQ实现异步消息处理、消费去重、任务重试和死信队列"),
        ("视觉", "训练图像分类与目标检测模型，处理图像标注和视觉数据增强"),
        ("前端", "使用React设计前端组件，完成表单交互、用户界面与可访问性改进"),
        ("语言", "实现自然语言分类、客户对话理解、文本抽取与语义匹配评测"),
    ]
    generations = []
    for batch in range(3):
        records = [record(f"synthetic-{batch}-{i}",
                          topics[i % 8][1] + f"，针对第{batch * 80 + i + 1}类公开合成需求完成设计。")
                   for i in range(80)]
        source = service.put_source(owner, records=records, title=f"public synthetic benchmark {batch}")
        generations.append(service.index_source(owner, source["source_revision_id"])["generation_id"])
    results = []
    for query in ("智能体工具调用和失败恢复", "语义向量检索与关键词融合", "Redis缓存限流热点", "异步消息去重重试"):
        params = {"user": owner, "generations": generations, "vector": vector_literal(embedder.embed_query(query))}
        predicate = "user_id=:user AND generation_id=ANY(CAST(:generations AS text[]))"
        ann_sql = "SELECT chunk_id FROM rag.chunk_vectors_bge512_v1 WHERE " + predicate + \
                  " ORDER BY embedding <=> CAST(:vector AS vector) LIMIT 20"
        with rag_db.transaction(owner) as cx:
            cx.execute(text("SET LOCAL hnsw.ef_search=100"))
            cx.execute(text("SET LOCAL hnsw.iterative_scan='strict_order'"))
            cx.execute(text("SET LOCAL hnsw.max_scan_tuples=20000"))
            # Deliberately exercise ANN even when PostgreSQL would choose an
            # exact B-tree scan for this small tenant. Report this explicitly.
            cx.execute(text("SET LOCAL enable_seqscan=off"))
            cx.execute(text("SET LOCAL enable_sort=off"))
            plan = cx.execute(text("EXPLAIN (FORMAT JSON) " + ann_sql), params).scalar_one()
            assert "rag_vectors_hnsw" in str(plan)
            ann = list(cx.execute(text(ann_sql), params).scalars())
        with rag_db.transaction(owner) as cx:
            exact = list(cx.execute(text("WITH scope AS MATERIALIZED (SELECT chunk_id,embedding "
                "FROM rag.chunk_vectors_bge512_v1 WHERE " + predicate + ") SELECT chunk_id "
                "FROM scope ORDER BY embedding <=> CAST(:vector AS vector) LIMIT 20"), params).scalars())
        assert len(exact) == 20
        recall = len(set(ann).intersection(exact)) / len(exact)
        results.append(recall)
        assert recall >= 0.95
    print("REAL_ANN_BENCHMARK", json.dumps({"documents": 240, "queries": 4, "k": 20,
          "raw_ann_recall": results, "mean_recall": sum(results) / len(results),
          "model_revision": embedder.profile["model_revision"], "forced_ann_plan": True,
          "synthetic_only": True}, ensure_ascii=False))
