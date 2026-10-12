"""Actual A -> B -> C path. No live resume, web page or remote model calls."""
import os
import time
import uuid
from urllib.parse import unquote, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.backend_v2.api import create_app
from app.backend_v2.auth import Accounts
from app.backend_v2.documents import DocumentStore
from app.backend_v2.messaging import OutboxPublisher
from app.backend_v2.settings import Settings
from app.backend_v2.workers import confirmed_publish, create_celery_app


def test_real_api_outbox_broker_workers_embedding_and_cached_search(v2_db, tmp_path, monkeypatch):
    broker = os.getenv("ZHIDA_TEST_BROKER_URL")
    control_url, cache_url = os.getenv("ZHIDA_TEST_REDIS_URL"), os.getenv("ZHIDA_TEST_CACHE_REDIS_URL")
    if not broker or not control_url or not cache_url or os.getenv("ZHIDA_TEST_REAL_EMBEDDING") != "1":
        pytest.skip("This path requires explicit real broker, both Redis and local embedding opt-ins")
    if not unquote(urlparse(broker).path).lstrip("/").startswith("zhida_test"):
        pytest.fail("Use a dedicated zhida_test* RabbitMQ vhost")
    from celery.contrib.testing.worker import start_worker
    monkeypatch.setenv("ZHIDA_V2_FILE_ROOT", str(tmp_path / "documents"))
    accounts = Accounts(v2_db)
    email = uuid.uuid4().hex + "@synthetic.invalid"
    owner = accounts.register(email, "Synthetic-password123!", "synthetic")["id"]
    auth = {"Authorization": "Bearer " + accounts.login(email, "Synthetic-password123!")["token"]}
    config = Settings(database_url=os.environ["ZHIDA_TEST_DATABASE_URL"], redis_cache_url=cache_url,
                      redis_control_url=control_url, cookie_secure=False, environment="test-" + uuid.uuid4().hex)
    app = create_celery_app(database_url=config.database_url, broker_url=broker)
    try:
        with TestClient(create_app(config, db=v2_db)) as client:
            created = client.post("/api/v2/resumes", headers=auth, json={"name": "合成Agent简历",
                "profile": {"projects": [{"name": "检索智能体", "description": "开发RAG证据检索系统，使用向量索引和工具调用实现Agent任务编排。"}],
                            "education": [{"school": "合成大学", "degree": "硕士", "major": "计算机"}]}})
            assert created.status_code == 201, created.text
            saved = created.json()
            document = tmp_path / "synthetic.txt"
            document.write_text("这只是自动化测试的合成简历：开发Agent工具调用。", encoding="utf-8")
            parsing = DocumentStore(v2_db).ingest(owner, document)
            with v2_db.transaction(owner) as cx:
                indexing = cx.execute(text("""SELECT id FROM zhida.background_jobs
                    WHERE user_id=:u AND kind='rag.index' AND aggregate_id=:r"""),
                    {"u": owner, "r": saved["index_source"]["source_revision_id"]}).scalar_one()
            with start_worker(app, perform_ping_check=False, pool="solo", concurrency=1,
                              queues=["document.parse", "rag.index"], loglevel="ERROR"):
                publisher = OutboxPublisher(v2_db, lambda event: confirmed_publish(app, event))
                deadline = time.monotonic() + 50
                states = []
                while time.monotonic() < deadline:
                    publisher.publish_batch(limit=100)
                    with v2_db.transaction(owner) as cx:
                        states = cx.execute(text("""SELECT state FROM zhida.background_jobs
                            WHERE user_id=:u AND id=ANY(:ids)"""),
                            {"u": owner, "ids": [indexing, parsing["job_id"]]}).scalars().all()
                    if states == ["completed", "completed"]:
                        break
                    if "failed" in states:
                        pytest.fail("A real handler failed: inspect scoped job reason")
                    time.sleep(0.2)
                assert states == ["completed", "completed"]
            query = {"query": "智能体RAG工具调用", "resume_revision_id": saved["revision_id"]}
            result = client.post("/api/v2/knowledge/search", headers=auth, json=query)
            assert result.status_code == 200, result.text
            evidence = result.json()["evidence"]
            assert evidence and evidence[0]["entity_type"] == "project"
            assert all(item["auto_fill_allowed"] is False for item in evidence)
            again = client.post("/api/v2/knowledge/search", headers=auth, json=query)
            assert again.status_code == 200 and again.json()["evidence"][0]["chunk_id"] == evidence[0]["chunk_id"]
            parsed = client.get("/api/v2/documents/" + parsing["file_id"] + "/extraction", headers=auth)
            assert parsed.status_code == 200 and parsed.json()["status"] == "needs_review"
            # Completion is bounded work, not a fabricated browser application success.
            assert client.get("/api/v2/health").json()["browser_writes_enabled"] is False
    finally:
        app.close()
