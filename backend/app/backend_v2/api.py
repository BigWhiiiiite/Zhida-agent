"""Durable data/tasks and retrieval, with explicitly gated D1 workflow commands."""
from __future__ import annotations

import ipaddress
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .auth import Accounts
from .db import Database
from .embedding import EmbeddingUnavailable
from .messaging import enqueue_job
from .rag import RagService, SourceConflict
from .redis_control import CacheBusy, ControlUnavailable, RedisControl, VersionedCache
from .repository import Conflict, NotFound, Repository, require_row
from .settings import Settings


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Payload):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=100)

    @field_validator("email")
    @classmethod
    def email_format(cls, value):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("邮箱格式不正确")
        return value.strip().casefold()


class ResumeInput(Payload):
    name: str = Field(min_length=1, max_length=200)
    profile: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=1)


class RunInput(Payload):
    resume_revision_id: str = Field(min_length=1, max_length=200)
    application_url: str = Field(min_length=1, max_length=3000)
    idempotency_key: str = Field(min_length=8, max_length=200)
    browser_session_id: str | None = Field(default=None, max_length=200)


class RevisionInput(Payload):
    expected_revision: int = Field(ge=1)


class ObservationInput(RevisionInput):
    browser_session_id: str = Field(min_length=1, max_length=200)
    connection_epoch: int = Field(ge=1)
    document_id: str = Field(min_length=1, max_length=200)
    capture_id: str = Field(min_length=1, max_length=200)
    captured_at: str = Field(min_length=1, max_length=100)
    snapshot: dict[str, Any]


class FactInput(Payload):
    subject_id: str = Field(default="person", min_length=1, max_length=200)
    semantic_key: str = Field(min_length=1, max_length=200)
    scope_key: str = Field(default="global", min_length=1, max_length=300)
    value: Any
    expected_revision: int = Field(default=0, ge=0)
    confirmation: Literal["confirmed", "unconfirmed", "revoked"] = "confirmed"
    valid_until: datetime | None = None
    question: str = Field(default="", max_length=1000)


class AnswerInput(RevisionInput):
    value: Any
    expected_fact_revision: int = Field(default=0, ge=0)
    expected_question_revision: int | None = Field(default=None, ge=1)


class SourceRecord(Payload):
    entity_id: str = Field(min_length=1, max_length=200)
    entity_type: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=120)
    text: str = Field(min_length=1, max_length=30000)
    source_path: str = Field(min_length=1, max_length=500)


class SourceInput(Payload):
    records: list[SourceRecord] = Field(min_length=1, max_length=100)
    # Confirmed-answer sources are written only by the versioned fact service.
    source_kind: Literal["user_note"] = "user_note"
    title: str = Field(default="", max_length=500)
    resume_revision_id: str | None = Field(default=None, max_length=200)
    site_scope: str | None = Field(default=None, max_length=300)
    expected_revision: int | None = Field(default=None, ge=0)

    @field_validator("site_scope")
    @classmethod
    def normalize_site(cls, value):
        from .repository import canonical_scope
        return canonical_scope("site:" + value)[len("site:"):] if value else None


class SearchInput(Payload):
    query: str = Field(min_length=1, max_length=3000)
    resume_revision_id: str | None = Field(default=None, max_length=200)
    source_ids: list[str] | None = Field(default=None, max_length=100)
    entity_ids: list[str] | None = Field(default=None, max_length=100)
    site_scope: str | None = Field(default=None, max_length=300)
    limit: int = Field(default=5, ge=1, le=10)

    @field_validator("site_scope")
    @classmethod
    def normalize_site(cls, value):
        return SourceInput.normalize_site(value)


def _token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    return authorization[7:] if authorization.startswith("Bearer ") else request.cookies.get("zhida_v2_session", "")


def _rate(request: Request, identity: str, group: str, capacity: float, refill: float) -> None:
    control = request.app.state.control
    if control is None:
        raise HTTPException(503, "限流服务未配置，暂停新请求")
    decision = control.rate_limit(user_id=identity, route_group=group, capacity=capacity, refill_per_second=refill)
    if not decision.allowed:
        raise HTTPException(429, "请求过于频繁，请稍后重试", headers={"Retry-After": "2"})


def identity(request: Request) -> str:
    config = request.app.state.config
    user_id = Accounts(request.app.state.db).authenticate(_token(request))
    if not user_id and config.local_user_enabled and config.environment == "development":
        try:
            loopback = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            loopback = False
        if loopback:
            user_id = config.local_user_id
            Repository(request.app.state.db).ensure_user(user_id, display_name="本机用户")
    if not user_id:
        raise HTTPException(401, "请登录 V2 后端")
    _rate(request, user_id, "read" if request.method == "GET" else "write",
          20 if request.method == "GET" else 5, 2 if request.method == "GET" else .5)
    return user_id


def create_app(config: Settings | None = None, *, db=None, control=None, embedder=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        cfg = config or Settings.from_env()
        if cfg.assisted_enabled and not (cfg.workflow_enabled and cfg.recognition_enabled):
            raise ValueError('D3 辅助填写需要同时启用 WORKFLOW_ENABLED 与 RECOGNITION_ENABLED')
        if cfg.recognition_enabled and not cfg.workflow_enabled:
            raise ValueError('只读识别 D2 需要同时启用 WORKFLOW_ENABLED')
        owned_database = db is None
        database = db or Database(cfg.database_url, pool_size=cfg.pool_size, pool_timeout=cfg.pool_timeout)
        if cfg.assisted_enabled:
            with database.transaction() as cx:
                ready=cx.execute(text("SELECT to_regclass('zhida.assisted_rounds') IS NOT NULL")).scalar_one()
            if not ready:
                if owned_database: database.close()
                raise ValueError('启用 D3 前请显式完成 0009_assisted_runtime 迁移')
        if cfg.workflow_enabled:
            # No automatic migration, and no misleading enabled health response
            # on an older schema. The D1 worker uses these tables transactionally.
            with database.transaction() as cx:
                ready = cx.execute(text("""SELECT
                    to_regclass('zhida.workflow_checkpoints') IS NOT NULL AND
                    to_regclass('zhida.workflow_heads') IS NOT NULL AND
                    to_regclass('zhida.workflow_step_commits') IS NOT NULL""")).scalar_one()
            if not ready:
                if owned_database:
                    database.close()
                raise ValueError("启用 D1 前请先显式完成 0007_workflow_runtime 迁移")
        if cfg.recognition_enabled:
            with database.transaction() as cx:
                ready = cx.execute(text("SELECT to_regclass('zhida.form_analysis_reports') IS NOT NULL")).scalar_one()
            if not ready:
                if owned_database:
                    database.close()
                raise ValueError('启用只读识别 D2 前请显式完成 0008_readonly_analysis 迁移')
        if cfg.environment == "production":
            with database.transaction() as cx:
                bypass = cx.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")).scalar_one()
            if bypass:
                if owned_database:
                    database.close()
                raise ValueError("生产 API 必须使用不能绕过 RLS 的运行角色")
        application.state.config, application.state.db = cfg, database
        clients = []
        if control is not None:
            application.state.control = control
        elif cfg.redis_control_url:
            from redis import Redis
            client = Redis.from_url(cfg.redis_control_url, socket_timeout=2, socket_connect_timeout=2)
            clients.append(client)
            application.state.control = RedisControl(client, prefix=f"zhida:v2:{cfg.environment}")
        else:
            application.state.control = None
        cache = None
        if cfg.redis_cache_url and application.state.control is not None:
            from redis import Redis
            cache_client = Redis.from_url(cfg.redis_cache_url, socket_timeout=2, socket_connect_timeout=2)
            clients.append(cache_client)
            cache = VersionedCache(cache_client, application.state.control, max_fallback=2)
        application.state.rag = RagService(database, embedder=embedder, query_cache=cache,
                                           query_cache_prefix=f"zhida:v2:{cfg.environment}:cache:qvec")
        # No implicit migrations, local user-data imports, model downloads or browser operations.
        yield
        for client in clients:
            client.close()
        if owned_database:
            database.close()

    application = FastAPI(title="Zhida durable backend A-C", version="1.0.0", lifespan=lifespan)
    application.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173",
        "http://127.0.0.1:4173", "http://localhost:4173"], allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type", "Authorization"])

    @application.middleware("http")
    async def same_origin_and_size(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            allowed = {"http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:4173", "http://localhost:4173",
                       str(request.base_url).rstrip("/")}
            if origin not in allowed:
                return JSONResponse({"detail": "来源不被允许"}, status_code=403)
        # Enforce streamed bytes too: Content-Length is optional/untrusted.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1_000_000:
                    return JSONResponse({"detail": "请求体超过 1MB"}, status_code=413)
            request._body = bytes(body)
        return await call_next(request)

    @application.exception_handler(NotFound)
    async def missing(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @application.exception_handler(LookupError)
    async def source_missing(_, exc):
        return JSONResponse({"detail": "记录不存在或无权访问"}, status_code=404)

    for exception in (Conflict, SourceConflict):
        async def conflict_handler(_, exc):
            return JSONResponse({"detail": str(exc)}, status_code=409)
        application.add_exception_handler(exception, conflict_handler)

    @application.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @application.exception_handler(IntegrityError)
    async def constraint(_, exc):
        return JSONResponse({"detail": "记录已存在、版本冲突或关联记录无效"}, status_code=409)

    @application.exception_handler(SQLAlchemyError)
    async def database_failure(_, exc):
        return JSONResponse({"detail": "数据库暂不可用；没有回退到旧资料库"}, status_code=503)

    for exception in (ControlUnavailable, CacheBusy):
        async def control_failure(_, exc):
            return JSONResponse({"detail": "缓存或限流服务繁忙，稍后重试"}, status_code=503)
        application.add_exception_handler(exception, control_failure)

    @application.exception_handler(EmbeddingUnavailable)
    async def embedding_unavailable(_, exc):
        return JSONResponse({"detail": "固定版本的本地向量模型不可用，请检查模型缓存；未尝试联网下载"}, status_code=503)

    @application.get("/api/v2/health")
    def health(request: Request):
        with request.app.state.db.transaction() as cx:
            revision = cx.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        cfg = request.app.state.config
        enabled = cfg.workflow_enabled
        return {"status": "ok", "schema_revision": revision,
                "workflow": "langgraph-d2-v1" if cfg.recognition_enabled else "langgraph-d1-v1" if enabled else "foundation-v1",
                "langgraph_enabled": enabled, "browser_writes_enabled": False,
                "workflow_stage": "d2_read_only" if cfg.recognition_enabled else "d1_control_only",
                "recognition_enabled": cfg.recognition_enabled, "workflow_worker_checked": False}

    @application.post("/api/v2/auth/register", status_code=201)
    def register(payload: Credentials, request: Request):
        _rate(request, f"anonymous:{request.client.host}", "auth", 5, 1 / 30)
        return Accounts(request.app.state.db).register(payload.email, payload.password.get_secret_value(), payload.display_name)

    @application.post("/api/v2/auth/login")
    def login(payload: Credentials, request: Request, response: Response):
        _rate(request, f"anonymous:{request.client.host}", "auth", 5, 1 / 30)
        session = Accounts(request.app.state.db).login(payload.email, payload.password.get_secret_value())
        if not session:
            raise HTTPException(401, "账号或密码不正确")
        response.set_cookie("zhida_v2_session", session["token"], httponly=True, samesite="strict",
                            secure=request.app.state.config.cookie_secure, max_age=7 * 86400)
        return session

    @application.post("/api/v2/auth/logout", status_code=204)
    def logout(request: Request, response: Response, user: str = Depends(identity)):
        Accounts(request.app.state.db).logout(_token(request))
        response.delete_cookie("zhida_v2_session")

    @application.get("/api/v2/resumes")
    def resumes(request: Request, user: str = Depends(identity)):
        return Repository(request.app.state.db).list_resumes(user)

    @application.post("/api/v2/resumes", status_code=201)
    def add_resume(payload: ResumeInput, request: Request, user: str = Depends(identity)):
        return Repository(request.app.state.db).save_resume(user, name=payload.name, profile=payload.profile)

    @application.put("/api/v2/resumes/{resume_id}")
    def edit_resume(resume_id: str, payload: ResumeInput, request: Request, user: str = Depends(identity)):
        return Repository(request.app.state.db).save_resume(user, resume_id=resume_id, **payload.model_dump())

    @application.get("/api/v2/facts")
    def facts(request: Request, include_unconfirmed: bool = False, user: str = Depends(identity)):
        return Repository(request.app.state.db).facts(user, include_unconfirmed=include_unconfirmed)

    @application.post("/api/v2/facts")
    def fact(payload: FactInput, request: Request, user: str = Depends(identity)):
        return Repository(request.app.state.db).save_fact(user, **payload.model_dump())

    @application.post("/api/v2/runs", status_code=202)
    def create_run(payload: RunInput, request: Request, user: str = Depends(identity)):
        _rate(request, user, "run_create", 2, 2 / 60)
        cfg = request.app.state.config
        workflow = ("langgraph-d2-v1" if cfg.recognition_enabled else
                    "langgraph-d1-v1" if cfg.workflow_enabled else "foundation-v1")
        return Repository(request.app.state.db).create_run(user, **payload.model_dump(), workflow_version=workflow)

    @application.get("/api/v2/runs/{run_id}")
    def read_run(run_id: str, request: Request, user: str = Depends(identity)):
        return Repository(request.app.state.db).get_run(user, run_id)

    @application.post('/api/v2/runs/{run_id}/observations', status_code=202)
    def observation(run_id: str, payload: ObservationInput, request: Request, user: str = Depends(identity)):
        if not request.app.state.config.recognition_enabled:
            raise HTTPException(409, '只读识别 D2 尚未启用')
        from .observation_store import ObservationStore
        return ObservationStore(request.app.state.db).record(user, run_id, **payload.model_dump())

    @application.get('/api/v2/runs/{run_id}/analysis')
    def analysis(run_id: str, request: Request, user: str = Depends(identity)):
        if not request.app.state.config.recognition_enabled:
            raise HTTPException(409, '只读识别 D2 尚未启用')
        from .observation_store import ObservationStore
        return ObservationStore(request.app.state.db).report(user, run_id)

    @application.get("/api/v2/runs/{run_id}/events")
    def events(run_id: str, request: Request, after: int = Query(default=0, ge=0), user: str = Depends(identity)):
        return Repository(request.app.state.db).events(user, run_id, after)

    @application.post("/api/v2/runs/{run_id}/cancel")
    def cancel(run_id: str, payload: RevisionInput, request: Request, user: str = Depends(identity)):
        return Repository(request.app.state.db).cancel_run(user, run_id, payload.expected_revision)

    @application.post("/api/v2/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str, payload: RevisionInput, request: Request, user: str = Depends(identity)):
        if not request.app.state.config.workflow_enabled:
            raise HTTPException(409, "可恢复工作流尚未启用；未派发任何浏览器操作")
        return Repository(request.app.state.db).resume_run(user, run_id, payload.expected_revision)

    @application.post("/api/v2/runs/{run_id}/answers/{confirmation_id}")
    def answer(run_id: str, confirmation_id: str, payload: AnswerInput, request: Request, user: str = Depends(identity)):
        return Repository(request.app.state.db).answer(user, run_id, confirmation_id, **payload.model_dump())

    def source_write(request, user, payload, source_id=None):
        with request.app.state.db.transaction(user) as cx:
            if source_id:
                require_row(cx, """SELECT id FROM rag.knowledge_sources
                    WHERE id=:id AND user_id=:u AND source_kind='user_note'""", {"id": source_id, "u": user})
            if payload.resume_revision_id:
                require_row(cx, "SELECT id FROM zhida.resume_revisions WHERE id=:id AND user_id=:u",
                            {"id": payload.resume_revision_id, "u": user})
            result = request.app.state.rag.put_source(user, source_id=source_id, conn=cx, **payload.model_dump())
            job = enqueue_job(cx, user_id=user, kind="rag.index", aggregate_id=result["source_revision_id"],
                              idempotency_key=f"index:{result['source_revision_id']}",
                              payload={"source_revision_id": result["source_revision_id"]})
            return {**result, "job_id": job.job_id, "index_status": "queued"}

    @application.post("/api/v2/knowledge/sources", status_code=202)
    def add_source(payload: SourceInput, request: Request, user: str = Depends(identity)):
        return source_write(request, user, payload)

    @application.put("/api/v2/knowledge/sources/{source_id}", status_code=202)
    def edit_source(source_id: str, payload: SourceInput, request: Request, user: str = Depends(identity)):
        return source_write(request, user, payload, source_id)

    @application.delete("/api/v2/knowledge/sources/{source_id}")
    def revoke(source_id: str, request: Request, user: str = Depends(identity)):
        with request.app.state.db.transaction(user) as cx:
            require_row(cx, """SELECT id FROM rag.knowledge_sources
                WHERE id=:id AND user_id=:u AND source_kind='user_note'""", {"id": source_id, "u": user})
            return request.app.state.rag.revoke_source(user, source_id, conn=cx)

    @application.post("/api/v2/knowledge/search")
    def search(payload: SearchInput, request: Request, user: str = Depends(identity)):
        if payload.resume_revision_id:
            with request.app.state.db.transaction(user) as cx:
                require_row(cx, "SELECT id FROM zhida.resume_revisions WHERE id=:id AND user_id=:u",
                            {"id": payload.resume_revision_id, "u": user})
        return request.app.state.rag.search(user, **payload.model_dump())

    @application.get("/api/v2/jobs/{job_id}")
    def job(job_id: str, request: Request, user: str = Depends(identity)):
        with request.app.state.db.transaction(user) as cx:
            return require_row(cx, """SELECT id,kind,state,attempt,result,last_error,available_at
                FROM zhida.background_jobs WHERE id=:id AND user_id=:u""", {"id": job_id, "u": user})

    @application.get("/api/v2/documents/{file_id}/extraction")
    def extraction(file_id: str, request: Request, user: str = Depends(identity)):
        with request.app.state.db.transaction(user) as cx:
            return require_row(cx, """SELECT id,file_id,parser_version,blocks,warnings,status
                FROM zhida.parsed_documents WHERE user_id=:u AND file_id=:id
                ORDER BY created_at DESC LIMIT 1""", {"u": user, "id": file_id})

    return application


app = create_app()
