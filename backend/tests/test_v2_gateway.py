"""Gateway contract tests use synthetic apps; no old database or .env is read."""
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.backend_v2.gateway import create_gateway, native_write_blocked, validate_cutover_environment


def test_gateway_requires_postgresql_and_distinct_uploads(monkeypatch, tmp_path):
    monkeypatch.delenv("ZHIDA_STORAGE_BACKEND", raising=False)
    with pytest.raises(RuntimeError, match="SQLite fallback"):
        validate_cutover_environment()
    monkeypatch.setenv("ZHIDA_STORAGE_BACKEND", "postgresql")
    monkeypatch.delenv("ZHIDA_UPLOAD_DIR", raising=False)
    with pytest.raises(RuntimeError, match="separate ZHIDA_UPLOAD_DIR"):
        validate_cutover_environment()
    monkeypatch.setenv("ZHIDA_UPLOAD_DIR", str(tmp_path / "v2-uploads"))
    validate_cutover_environment()


@pytest.mark.parametrize("path", ["/api/v2/resumes", "/api/v2/resumes/example", "/api/v2/facts",
    "/api/v2/knowledge/sources", "/api/v2/knowledge/sources/example", "/api/v2/runs", "/api/v2/runs/x/cancel"])
def test_native_dual_writes_blocked(path):
    assert native_write_blocked(path, "POST")
    assert native_write_blocked(path, "DELETE")
    assert not native_write_blocked(path, "GET")
    assert not native_write_blocked(path, "OPTIONS")
    assert not native_write_blocked("/api/v2/knowledge/search", "POST")


def test_d1_flag_only_opens_run_lifecycle_not_fact_writes():
    for path in ('/api/v2/runs', '/api/v2/runs/id/resume', '/api/v2/runs/id/cancel'):
        assert not native_write_blocked(path, 'POST', workflow_enabled=True)
    for path in ('/api/v2/facts', '/api/v2/resumes', '/api/v2/knowledge/sources',
                 '/api/v2/runs/id/answers', '/api/v2/runs/id/anything', '/api/v2/runs/id/resume/more'):
        assert native_write_blocked(path, 'POST', workflow_enabled=True)
    assert native_write_blocked('/api/v2/runs', 'DELETE', workflow_enabled=True)


def test_gateway_preserves_paths_auth_and_each_app_lifespan(monkeypatch, tmp_path):
    monkeypatch.setenv("ZHIDA_STORAGE_BACKEND", "postgresql")
    monkeypatch.setenv("ZHIDA_UPLOAD_DIR", str(tmp_path / "uploads"))
    events = []

    @asynccontextmanager
    async def native_lifespan(app):
        events.append("native-start")
        db = MagicMock()
        db.transaction.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = "test-head"
        app.state.db = db
        yield
        events.append("native-stop")

    @asynccontextmanager
    async def legacy_lifespan(app):
        events.append("compat-start")
        yield
        events.append("compat-stop")

    native = FastAPI(lifespan=native_lifespan)
    legacy = FastAPI(lifespan=legacy_lifespan)

    @native.get("/api/v2/facts")
    def facts():
        return {"source": "native"}

    @native.post("/api/v2/knowledge/search")
    def search():
        return {"source": "native-rag"}

    @legacy.get("/api/profile")
    def profile():
        return {"source": "compat"}

    @legacy.get("/api/auth/me")
    def me(request: Request):
        return {"cookie": request.cookies.get("zhida_session"), "path": request.url.path}

    @legacy.post("/api/resumes")
    async def upload(request: Request):
        return {"bytes": len(await request.body())}

    with TestClient(create_gateway(legacy=legacy, native=native), base_url="http://127.0.0.1:18000") as client:
        assert events == ["native-start", "compat-start"]
        status = client.get("/api/health").json()
        assert status["storage"] == "postgresql"
        assert status["backend_generation"] == "v2"
        assert status["langgraph_enabled"] is False
        assert client.get("/api/profile").json() == {"source": "compat"}
        assert client.get("/api/v2/facts").json() == {"source": "native"}
        assert client.post("/api/v2/facts", json={}).status_code == 409
        assert client.post("/api/v2/knowledge/search", json={}).json() == {"source": "native-rag"}
        client.cookies.set("zhida_session", "synthetic-session")
        assert client.get("/api/v2/auth/me").json() == {"cookie": "synthetic-session", "path": "/api/auth/me"}
        # The native API's 1MB JSON cap must not cover compatibility file upload.
        assert client.post("/api/resumes", content=b"x" * 1_100_000).json() == {"bytes": 1_100_000}
        assert client.post("/api/resumes", content=b"x", headers={"Origin": "https://untrusted.example"}).status_code == 403
        assert client.get("/api/health", headers={"Host": "untrusted.example"}).status_code == 403
        status_response = client.get("/api/health", headers={"Origin": "http://127.0.0.1:4173"})
        assert status_response.headers["cache-control"] == "no-store"
        assert status_response.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
        assert status_response.headers["x-content-type-options"] == "nosniff"
        assert client.options("/api/resumes/x", headers={"Origin": "http://127.0.0.1:4173",
            "Access-Control-Request-Method": "PATCH", "Access-Control-Request-Headers": "content-type"}).status_code == 200
    assert events == ["native-start", "compat-start", "compat-stop", "native-stop"]
