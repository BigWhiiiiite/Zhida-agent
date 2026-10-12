"""PostgreSQL cutover gateway retaining the workbench's existing API contracts.

The compatibility app owns profile writes during this transition. An explicit
D1 flag can enable run lifecycle commands, never duplicate fact writes or the
native browser Runner. Existing workbench actions still use their old path.
No HTTP fallback or proxy to the old SQLite backend exists here.
"""
from __future__ import annotations

import os
import ipaddress
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .api import _rate, create_app as create_native_app, identity


def native_write_blocked(path: str, method: str, *, workflow_enabled: bool = False) -> bool:
    if method in {"GET", "HEAD", "OPTIONS"}:
        return False
    if workflow_enabled and method == 'POST':
        parts = path.strip('/').split('/')
        if parts == ['api', 'v2', 'runs'] or (len(parts) == 5 and
                parts[:3] == ['api', 'v2', 'runs'] and parts[4] in {'resume', 'cancel'}):
            return False
    roots = ("/api/v2/resumes", "/api/v2/facts", "/api/v2/knowledge/sources", "/api/v2/runs")
    return any(path == root or path.startswith(root + "/") for root in roots)


def validate_cutover_environment() -> None:
    if os.getenv("ZHIDA_STORAGE_BACKEND", "").lower() != "postgresql":
        raise RuntimeError("V2 gateway requires ZHIDA_STORAGE_BACKEND=postgresql; SQLite fallback is forbidden")
    upload_path = os.getenv("ZHIDA_UPLOAD_DIR", "")
    original = Path(__file__).resolve().parents[2] / "uploads"
    if not upload_path or Path(upload_path).expanduser().resolve() == original.resolve():
        raise RuntimeError("V2 gateway requires a separate ZHIDA_UPLOAD_DIR; original uploads must remain untouched")


def create_gateway(*, legacy=None, native=None) -> FastAPI:
    # The legacy import is deferred until startup configuration has been checked.
    native_app = native or create_native_app()
    apps: dict[str, object] = {"native": native_app}

    def workflow_enabled():
        return bool(getattr(getattr(native_app.state, 'config', None), 'workflow_enabled', False))

    def recognition_enabled():
        return bool(getattr(getattr(native_app.state, 'config', None), 'recognition_enabled', False))

    def assisted_enabled():
        return bool(getattr(getattr(native_app.state,'config',None),'assisted_enabled',False))

    @asynccontextmanager
    async def lifespan(application):
        validate_cutover_environment()
        if legacy is None:
            from .. import main as legacy_module
            legacy_app = legacy_module.app

            def legacy_identity(request: Request) -> str:
                user = legacy_module.user_for_token(request.cookies.get(legacy_module.SESSION_COOKIE, ""))
                if not user and legacy_module._local_access_allowed(request):
                    user = legacy_module.local_user()
                if not user:
                    raise HTTPException(401, "请登录职达")
                _rate(request, user.id, "read" if request.method == "GET" else "write",
                      20 if request.method == "GET" else 5, 2 if request.method == "GET" else .5)
                return user.id

            native_app.dependency_overrides[identity] = legacy_identity
        else:
            legacy_app = legacy
        apps["legacy"] = legacy_app
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(native_app.router.lifespan_context(native_app))
            await stack.enter_async_context(legacy_app.router.lifespan_context(legacy_app))
            application.state.native = native_app
            yield

    gateway = FastAPI(title="Zhida PostgreSQL workbench gateway", lifespan=lifespan,
                      docs_url=None, redoc_url=None, openapi_url=None)
    origins = {"http://127.0.0.1:5173", "http://localhost:5173",
               "http://127.0.0.1:4173", "http://localhost:4173"}
    gateway.add_middleware(CORSMiddleware, allow_origins=sorted(origins), allow_credentials=True,
                          allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                          allow_headers=["Content-Type", "Authorization"])

    @gateway.middleware("http")
    async def local_gateway_boundary(request: Request, call_next):
        # This transitional gateway owns the local desktop browser. It is not
        # the multi-tenant server endpoint; do not expose it via LAN/proxy.
        client_host = request.client.host if request.client else ""
        try:
            local = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            local = client_host == "testclient"
        if not local or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse({"detail": "本机工作台网关只接受本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            if origin not in origins | {str(request.base_url).rstrip("/")}:
                return JSONResponse({"detail": "请求来源不被允许"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @gateway.get("/api/health")
    @gateway.get("/api/backend-status")
    def backend_status(request: Request):
        db = request.app.state.native.state.db
        with db.transaction() as cx:
            schema_revision = cx.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        return {"status": "ok", "product": "Zhida", "storage": "postgresql",
                "backend_generation": "v2", "compatibility": True,
                "schema_revision": schema_revision, "langgraph_enabled": workflow_enabled(),
                "native_workflow_enabled": workflow_enabled(),
                "workflow_stage": "d3_local_runner" if assisted_enabled() else "d2_read_only" if recognition_enabled() else "d1_control_only",
                "recognition_enabled": recognition_enabled(),
                "browser_writes_enabled": assisted_enabled(), "workflow_worker_checked": False,
                "legacy_workbench_enabled": True,
                "workbench_workflow": "langgraph_d3_local_runner" if assisted_enabled() else "legacy_compatibility"}

    async def dispatch(scope, receive, send):
        path = scope.get("path", "")
        if path.startswith("/api/v2/") or path == "/api/v2":
            if native_write_blocked(path, scope.get("method", "GET"), workflow_enabled=workflow_enabled()):
                response = JSONResponse({"detail": "过渡期请从职达工作台修改资料；原生 V2 写接口暂未开放，避免两套数据发生冲突"}, status_code=409)
                return await response(scope, receive, send)
            if path.startswith("/api/v2/auth/"):
                # Same identity and cookies for both API families, never two logins.
                auth_scope = dict(scope)
                auth_scope["path"] = path.replace("/api/v2/auth/", "/api/auth/", 1)
                auth_scope["raw_path"] = auth_scope["path"].encode()
                return await apps["legacy"](auth_scope, receive, send)
            return await native_app(scope, receive, send)
        return await apps["legacy"](scope, receive, send)

    gateway.mount("/", dispatch)
    return gateway


app = create_gateway()
