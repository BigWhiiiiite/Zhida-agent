import uuid

import pytest
from fastapi.testclient import TestClient

from app.backend_v2.api import create_app
from app.backend_v2.redis_control import RateDecision
from app.backend_v2.settings import Settings


class AllowedControl:
    def rate_limit(self, **kwargs):
        return RateDecision(True, 10, 0)


def account(client):
    credentials = {"email": uuid.uuid4().hex + "@synthetic.invalid", "password": "SynthetiC-password!234"}
    response = client.post("/api/v2/auth/register", json=credentials)
    assert response.status_code == 201, response.text
    response = client.post("/api/v2/auth/login", json=credentials)
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_api_auth_schema_idempotence_and_user_isolation(v2_db):
    cfg = Settings(database_url=str(v2_db.engine.url), cookie_secure=False)
    with TestClient(create_app(cfg, db=v2_db, control=AllowedControl())) as client:
        assert client.get("/api/v2/health").json()["langgraph_enabled"] is False
        assert client.get("/api/v2/resumes").status_code == 401
        first = account(client)
        response = client.post("/api/v2/resumes", json={"name": "合成简历", "profile": {}}, headers=first)
        assert response.status_code == 201, response.text
        revision = response.json()["revision_id"]
        payload = {"resume_revision_id": revision, "application_url": "https://recruit.synthetic.invalid/form",
                   "idempotency_key": "api-repeated-key"}
        created = client.post("/api/v2/runs", json=payload, headers=first)
        assert created.status_code == 202, created.text
        assert client.post("/api/v2/runs", json=payload, headers=first).json()["id"] == created.json()["id"]
        second = account(client)
        assert client.get("/api/v2/runs/" + created.json()["id"], headers=second).status_code == 404
        assert client.post("/api/v2/runs", json=payload, headers=second).status_code == 404
        assert client.post("/api/v2/facts", headers=second, json={"semantic_key": "gender", "value": "男", "user_id": "spoof"}).status_code == 422
        assert client.post("/api/v2/facts", headers={**first, "Origin": "https://malicious.invalid"},
                           json={"semantic_key": "gender", "value": "男"}).status_code == 403


def test_api_facts_and_source_updates_require_current_revision(v2_db):
    cfg = Settings(database_url=str(v2_db.engine.url), cookie_secure=False)
    with TestClient(create_app(cfg, db=v2_db, control=AllowedControl())) as client:
        auth = account(client)
        payload = {"semantic_key": "availability", "value": "是", "question": "是否可提前实习"}
        assert client.post("/api/v2/facts", json=payload, headers=auth).status_code == 200
        assert client.post("/api/v2/facts", json=payload, headers=auth).status_code == 409
        assert client.post("/api/v2/facts", json={**payload, "value": "否", "expected_revision": 1}, headers=auth).status_code == 200
        assert client.get("/api/v2/facts", headers=auth).json()[0]["value"] == "否"
        evidence = {"records": [{"entity_id": "example", "entity_type": "project", "title": "工具项目",
                                  "text": "Agent工具调用与检索", "source_path": "project:1"}]}
        source = client.post("/api/v2/knowledge/sources", headers=auth, json=evidence)
        assert source.status_code == 202, source.text
        path = "/api/v2/knowledge/sources/" + source.json()["source_id"]
        assert client.put(path, headers=auth, json=evidence).status_code == 409
        assert client.put(path, headers=auth, json={**evidence, "expected_revision": 1}).status_code == 202
        assert client.post("/api/v2/knowledge/sources", headers=auth,
                           json={**evidence, "source_kind": "confirmed_answer"}).status_code == 422
        fact_id = client.get("/api/v2/facts", headers=auth).json()[0]["id"]
        internal = "/api/v2/knowledge/sources/fact:" + fact_id
        assert client.put(internal, headers=auth, json={**evidence, "expected_revision": 2}).status_code == 404
        assert client.delete(internal, headers=auth).status_code == 404


def test_api_unconfigured_control_fails_closed(v2_db):
    with TestClient(create_app(Settings(database_url=str(v2_db.engine.url)), db=v2_db)) as client:
        response = client.post("/api/v2/auth/register", json={"email": "a@synthetic.invalid", "password": "abcdefgh"})
        assert response.status_code == 503


def test_missing_local_model_is_explicit_unavailable_not_internal_error(v2_db, tmp_path):
    from app.backend_v2.embedding import LocalBGEEmbedder
    cfg = Settings(database_url=str(v2_db.engine.url), cookie_secure=False)
    with TestClient(create_app(cfg, db=v2_db, control=AllowedControl(),
                               embedder=LocalBGEEmbedder(tmp_path / "absent-model"))) as client:
        auth = account(client)
        response = client.post("/api/v2/knowledge/search", headers=auth, json={"query": "合成查询"})
        assert response.status_code == 503 and "联网下载" in response.json()["detail"]
