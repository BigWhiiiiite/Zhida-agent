from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import csv
import io
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, Request, Response as FastAPIResponse, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from dotenv import load_dotenv

from .agent import get_resume_extractor
from .auth_models import AuthSession, LoginRequest, RegisterRequest, UserAccount
from .auth_service import SESSION_DAYS, local_user, login, register, token_hash, user_for_token
from .application_agent import decide_application_step
from .application_journey import continue_journey
from .application_knowledge import (KnowledgeCreate, KnowledgeRecord, MappingTarget,
                                    delete_knowledge, initialize as initialize_application_knowledge,
                                    list_knowledge, mapping_targets, save_knowledge)
from .application_models import (ApplicationAgentCheckpoint, ApplicationAgentStepRequest,
                                 ApplicationJourneyRequest, ApplicationJourneyResult,
                                 ApplicationAgentTurn, ApplicationWorkflowState,
                                 RegistrationCredentialsRequest, VerificationCodeRequest,
                                 VerificationRequest, WorkflowAdvanceRequest)
from .browser_models import (BrowserSnapshot, BrowserStart, ExecutePlanRequest, ExecutionResult,
                             ExpandSectionRequest, FormPlan, FormReviewResult,
                             HybridAutofillRequest, HybridAutofillResult,
                             NativeResumeImportRequest, NativeResumeImportResult, PreSubmitCheck)
from .browser_service import browser_demo, NavigationNoProgressError, configured_browser_engine
from .external_browser import (ExternalWebsiteOpen, ExistingSafariPreview,
                               ExistingSafariConfirmation, open_external_website)
from .safari_window_registry import safari_windows
from .task_profile import compose_task_profile, context_token
from .application_knowledge import company_scope
from .browser_models import TaskResumeUpdate
from .browser_models import ApplicationAssistRequest, ApplicationAssistResult, ApplicationAssistProgress
from .application_assist import prepare_application
from .assist_runs import assist_runs
from .chat_api import router as chat_router
from .chat_storage import initialize as initialize_chat
from .extractors import extract_text, preview_html
from .confirmed_facts import (ConfirmedFactRequest, FactRevisionConflict, FactTargets,
                              get_fact_targets, save_confirmed_fact)
from .form_agent import build_form_review, create_form_plan, create_local_form_plan
from .extraction_audit import (ExtractionAuditRequest, ExtractionAuditResult,
                               audit_extraction, document_fingerprint)
from .page_observation import PageObservationRequest, PageRegionObservation, ObservationConsentRequest
from .job_discovery import (official_job_sources, register_job_source,
                            remove_job_source, sync_official_source)
from .job_models import (ApplicationQueueItem, ApplicationQueueUpdate,
                         ApplicationReadiness, JobDiscoveryResult, JobEvidenceExplanation, JobVerification,
                         JobSourceCreate, OfficialJobSource, QueueAddRequest,
                         RagRecommendationRequest, RecommendationBatch, SmartJobSearchRequest,
                         SmartJobSearchResult)
from .job_recommendations import (add_to_queue, application_readiness, queue_items,
                                  catalog_job, rag_recommendation_batch,
                                  recommendation_batch, remove_from_queue,
                                  smart_job_search, update_queue_item,
                                  verify_catalog_job)
from .job_rag import explain_evidence_matches
from .models import (ApplicationAnswerUpdate, CandidateProfile, ConflictResolution, ExportBundle, FieldEvidence,
                     ModelHealth, ProfileConflict, ResumeProfile, ResumeRecord, ResumeUpdate, ReviewUpdate)
from .model_provider import check_model_health
from .profile_service import (apply_profile_value, build_evidence, detect_language, merge_into_profile,
                              save_application_answer, sync_edited_profile_value)
from .storage import (create_pending_resume, current_user_id, delete_resume, delete_session_record,
                      find_by_hash, get_conflict, get_profile,
                      get_resume, get_resume_internal, initialize, list_conflicts, list_resumes,
                      mark_resume_failed, mark_resume_parsing, replace_parse_result, reset_current_user,
                      resolve_conflict, save_application_agent_checkpoint, save_profile, set_current_user,
                      update_evidence, update_resume)


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)
UPLOAD_DIR = ROOT / "uploads"
ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt"}
CONTENT_TYPES = {
    ".pdf": {"application/pdf", "application/octet-stream"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip", "application/octet-stream"},
    ".txt": {"text/plain", "application/octet-stream"},
}
MAX_FILE_SIZE = 10 * 1024 * 1024
SESSION_COOKIE = "zhida_session"
PUBLIC_API_PATHS = {"/api/health", "/api/auth/register", "/api/auth/login"}
browser_session_owners: dict[str, str] = {}
browser_task_resumes: dict[str, str] = {}
browser_task_epochs: dict[str, str] = {}
browser_image_consents: dict[str, str] = {}  # task-context token, not a global preference


def _task_context(session_id: str) -> tuple[CandidateProfile, str]:
    resume_id = browser_task_resumes.get(session_id, "")
    resume = get_resume(resume_id) if resume_id else None
    if resume_id and not resume:
        raise HTTPException(409, "本次投递绑定的简历已删除，请重新选择简历")
    url = browser_demo.page.url if browser_demo.page else ""
    profile = compose_task_profile(get_profile(), resume, company_scope(url) if url else "")
    revision = "|".join((context_token(profile, resume), session_id, url,
                         browser_task_epochs.get(session_id, "")))
    return profile, hashlib.sha256(revision.encode()).hexdigest()


def _task_profile(session_id: str) -> CandidateProfile:
    return _task_context(session_id)[0]


def _stamp_plan(session_id: str, plan: FormPlan) -> FormPlan:
    plan.context_token = _task_context(session_id)[1]
    plan.resume_id = browser_task_resumes.get(session_id, "")
    if plan.resume_id:
        from .task_profile import VERSION_FIELDS
        resume = get_resume(plan.resume_id)
        for action in plan.actions:
            if any(action.value_source.startswith(f"主档案.{field}") for field in VERSION_FIELDS):
                action.value_source = action.value_source.replace("主档案.", f"简历「{resume.label}」.", 1)
    return plan


async def _model_task_plan(session_id: str, snapshot: BrowserSnapshot) -> FormPlan:
    profile, revision = _task_context(session_id)
    observation_workflow = await browser_demo.workflow_state(session_id)
    observation_snapshot = snapshot.model_copy(deep=True)
    observation_lock = asyncio.Lock()
    async def observe_controls(selectors):
        async with observation_lock:
            _require_browser_owner(session_id)
            if revision != _task_context(session_id)[1]:
                raise HTTPException(409, '资料已变化，停止模型控件观察')
            result = await browser_demo.observe_form_controls(session_id, selectors,
                expected_snapshot=observation_snapshot, expected_workflow=observation_workflow)
            if revision != _task_context(session_id)[1]:
                raise HTTPException(409, '观察期间资料已变化，丢弃结果')
            return result
    async def observe_page_region(selector):
        async with observation_lock:
            _require_browser_owner(session_id)
            if revision != _task_context(session_id)[1]:
                raise HTTPException(409, '资料已变化，停止题目观察')
            result = await browser_demo.observe_page_region(session_id, selector,
                include_image=browser_image_consents.get(session_id) == revision, expected_snapshot=snapshot)
            if revision != _task_context(session_id)[1]:
                raise HTTPException(409, '观察期间资料已变化，丢弃结果')
            return result
    plan = await create_form_plan(snapshot, profile, observe_controls=observe_controls,
                                  observe_page_region=observe_page_region)
    if revision != _task_context(session_id)[1]:
        raise HTTPException(409, "模型分析期间资料发生变化，请重新分析，未执行旧计划")
    return _stamp_plan(session_id, plan)


def _require_task_resume(session_id: str, resume_id: str) -> None:
    if session_id in browser_task_resumes and resume_id != browser_task_resumes[session_id]:
        raise HTTPException(409, "简历版本与当前任务不一致，请切换简历后重新分析")


def _local_access_allowed(request: Request) -> bool:
    auth_required = os.getenv("APP_AUTH_REQUIRED", "true").strip().lower() in {"1", "true", "yes"}
    if auth_required:
        return False
    client_host = request.client.host if request.client else ""
    if client_host == "testclient":
        return True
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return client_host == "localhost"


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize(); UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    initialize_application_knowledge()
    initialize_chat()
    app.state.browser_operation_lock = asyncio.Lock()
    yield
    await browser_demo.close()


app = FastAPI(title="职达 Zhida API", description="候选人资料、岗位推荐、简历解析与求职表单 Demo", version="0.4.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(chat_router)


@app.middleware("http")
async def require_account(request: Request, call_next):
    if request.method == "OPTIONS" or not request.url.path.startswith("/api/") or request.url.path in PUBLIC_API_PATHS:
        return await call_next(request)
    user = user_for_token(request.cookies.get(SESSION_COOKIE, ""))
    if not user and _local_access_allowed(request):
        user = local_user()
    if not user:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    request.state.user = user
    context_token = set_current_user(user.id)
    try:
        # Run receipts never touch the browser. They must remain readable while
        # the single browser operation lock is held by a long model/fill run.
        receipt_path = request.url.path.split('/')
        receipt_only = (len(receipt_path) in {7,8} and receipt_path[1:3] == ['api','browser']
            and receipt_path[4:6] == ['assist','progress']
            and (request.method == 'GET' and len(receipt_path) == 7
                 or request.method == 'POST' and len(receipt_path) == 8 and receipt_path[7] == 'cancel'))
        if not receipt_only and (request.url.path == "/api/browser" or request.url.path.startswith("/api/browser/")):
            # The current service owns one active browser. Background inspection
            # also opens menus, so serialize it with filling and navigation.
            async with app.state.browser_operation_lock:
                return await call_next(request)
        return await call_next(request)
    finally:
        reset_current_user(context_token)


def _set_session_cookie(response: FastAPIResponse, token: str) -> None:
    secure = os.getenv("APP_COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes"}
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 24 * 60 * 60,
                        httponly=True, secure=secure, samesite="lax", path="/")


def _require_browser_owner(session_id: str) -> None:
    if browser_session_owners.get(session_id) != current_user_id():
        raise HTTPException(404, "浏览器会话不存在")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "product": "Zhida", "browser_engine": configured_browser_engine()}


@app.post("/api/auth/register", response_model=AuthSession, status_code=201)
def register_account(payload: RegisterRequest, response: FastAPIResponse) -> AuthSession:
    try:
        user, token = register(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _set_session_cookie(response, token)
    return AuthSession(user=user)


@app.post("/api/auth/login", response_model=AuthSession)
def login_account(payload: LoginRequest, response: FastAPIResponse) -> AuthSession:
    try:
        user, token = login(payload)
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    _set_session_cookie(response, token)
    return AuthSession(user=user)


@app.get("/api/auth/me", response_model=UserAccount)
def current_account(request: Request) -> UserAccount:
    return request.state.user


@app.post("/api/auth/logout", status_code=204, response_class=Response)
async def logout_account(request: Request) -> Response:
    raw_token = request.cookies.get(SESSION_COOKIE, "")
    if raw_token:
        delete_session_record(token_hash(raw_token))
    owned_sessions = [session_id for session_id, user_id in browser_session_owners.items()
                      if user_id == current_user_id()]
    if browser_demo.session_id in owned_sessions:
        await browser_demo.close()
    safari_windows.forget_owner(current_user_id())
    for session_id in owned_sessions:
        browser_session_owners.pop(session_id, None)
        browser_image_consents.pop(session_id, None)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.post("/api/model/health", response_model=ModelHealth)
async def model_health() -> ModelHealth:
    return await check_model_health()


@app.get("/api/profile", response_model=CandidateProfile)
def profile() -> CandidateProfile: return get_profile()


@app.patch("/api/profile", response_model=CandidateProfile)
def update_profile(payload: ResumeProfile) -> CandidateProfile: return save_profile(payload)


@app.post("/api/profile/application-answer", response_model=CandidateProfile)
def remember_application_answer(payload: ApplicationAnswerUpdate) -> CandidateProfile:
    try:
        return save_application_answer(
            payload.question, payload.field_name, payload.value,
            semantic_key=payload.semantic_key, entity_scope=payload.entity_scope,
            field_signature=payload.field_signature, field_type=payload.field_type,
            options=payload.options, source_url=payload.source_url, resume_id=payload.resume_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/application-knowledge/targets", response_model=list[MappingTarget])
def application_mapping_targets() -> list[MappingTarget]:
    return mapping_targets()


@app.get("/api/application-knowledge", response_model=list[KnowledgeRecord])
def application_knowledge() -> list[KnowledgeRecord]:
    return list_knowledge()


@app.post("/api/application-knowledge", response_model=KnowledgeRecord, status_code=201)
def confirm_application_knowledge(payload: KnowledgeCreate) -> KnowledgeRecord:
    try:
        return save_knowledge(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/application-knowledge/{knowledge_id}", status_code=204)
def remove_application_knowledge(knowledge_id: str) -> FastAPIResponse:
    if not delete_knowledge(knowledge_id):
        raise HTTPException(404, "知识不存在或不属于当前用户")
    return FastAPIResponse(status_code=204)


@app.get("/api/jobs/recommendations", response_model=RecommendationBatch)
def job_recommendations(location: str = "") -> RecommendationBatch:
    return recommendation_batch(get_profile(), location)


@app.post("/api/jobs/recommendations/rag", response_model=RecommendationBatch)
def rag_job_recommendations(payload: RagRecommendationRequest) -> RecommendationBatch:
    return rag_recommendation_batch(
        get_profile(), payload.location, payload.query, payload.company_sizes,
    )


@app.post("/api/jobs/{job_id}/explain", response_model=JobEvidenceExplanation)
async def explain_job_recommendation(job_id: str) -> JobEvidenceExplanation:
    job = catalog_job(job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    batch = rag_recommendation_batch(get_profile(), query=job.title)
    item = next((candidate for candidate in batch.jobs if candidate.job.id == job_id), None)
    if item is None:
        raise HTTPException(404, "岗位暂不在当前推荐列表")
    return await explain_evidence_matches(item)


@app.get("/api/jobs/queue", response_model=list[ApplicationQueueItem])
def application_queue() -> list[ApplicationQueueItem]:
    return queue_items(get_profile())


@app.get("/api/readiness", response_model=ApplicationReadiness)
def readiness() -> ApplicationReadiness:
    return application_readiness(get_profile())


@app.get("/api/jobs/sources", response_model=list[OfficialJobSource])
def job_sources() -> list[OfficialJobSource]:
    return official_job_sources()


@app.post("/api/jobs/sources", response_model=OfficialJobSource, status_code=201)
def add_job_source(payload: JobSourceCreate) -> OfficialJobSource:
    try:
        return register_job_source(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/jobs/sources/{source_id}", status_code=204, response_class=Response)
def delete_job_source(source_id: str) -> Response:
    try:
        if not remove_job_source(source_id):
            raise HTTPException(404, "招聘来源不存在")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(status_code=204)


@app.post("/api/jobs/sources/{source_id}/sync", response_model=JobDiscoveryResult)
async def sync_job_source(source_id: str) -> JobDiscoveryResult:
    try:
        return await sync_official_source(source_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/jobs/search", response_model=SmartJobSearchResult)
async def search_official_jobs(payload: SmartJobSearchRequest) -> SmartJobSearchResult:
    return await smart_job_search(get_profile(), payload)


@app.post("/api/jobs/{job_id}/verify", response_model=JobVerification)
async def verify_job_opening(job_id: str) -> JobVerification:
    try:
        return await verify_catalog_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/jobs/queue", response_model=list[ApplicationQueueItem], status_code=201)
def add_application_queue(payload: QueueAddRequest) -> list[ApplicationQueueItem]:
    try:
        return add_to_queue(get_profile(), payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.patch("/api/jobs/queue/{queue_id}", response_model=ApplicationQueueItem)
def update_application_queue(queue_id: str,
                             payload: ApplicationQueueUpdate) -> ApplicationQueueItem:
    try:
        return update_queue_item(get_profile(), queue_id, payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/jobs/queue-export.csv")
def export_application_queue() -> Response:
    status_labels = {
        "planned": "计划中", "in_progress": "填写中", "needs_review": "待复核",
        "ready_to_submit": "待提交", "submitted": "已投递", "interview": "面试中",
        "offer": "Offer", "rejected": "未通过", "withdrawn": "已撤回",
    }
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["公司", "岗位", "职位编号", "地点", "状态", "匹配分", "申请编号",
                     "所用简历", "加入时间", "AI辅助开始时间", "投递时间", "备注", "官方链接"])
    for item in queue_items(get_profile()):
        job = item.recommendation.job
        writer.writerow([
            job.company, job.title, job.job_code, " / ".join(job.locations),
            status_labels[item.status], item.recommendation.match_score,
            item.application_id, item.resume_id, item.created_at.isoformat(),
            item.assistance_started_at.isoformat() if item.assistance_started_at else "",
            item.submitted_at.isoformat() if item.submitted_at else "",
            item.notes, job.source_url,
        ])
    content = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=zhida-applications.csv"},
    )


@app.delete("/api/jobs/queue/{queue_id}", status_code=204, response_class=Response)
def remove_application_queue(queue_id: str) -> Response:
    try:
        if not remove_from_queue(get_profile(), queue_id):
            raise HTTPException(404, "投递清单项目不存在")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(status_code=204)


@app.get("/api/resumes", response_model=list[ResumeRecord])
def resumes() -> list[ResumeRecord]: return list_resumes()


@app.get("/api/resumes/{resume_id}", response_model=ResumeRecord)
def resume(resume_id: str) -> ResumeRecord:
    record = get_resume(resume_id)
    if not record: raise HTTPException(404, "简历不存在")
    return record


RETRYABLE_MODEL_ERRORS = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)


def _retryable_parse_error(exc: Exception, resume_id: str, action: str) -> HTTPException:
    if isinstance(exc, RETRYABLE_MODEL_ERRORS):
        message = f"模型代理暂时不可用，简历文件已保留。请稍后在简历资料库点击“重新解析”。"
        return HTTPException(503, detail={"message": message, "resume_id": resume_id, "retryable": True},
                             headers={"Retry-After": "30"})
    return HTTPException(422, detail={"message": f"{action}：{exc}", "resume_id": resume_id, "retryable": True})


async def _parse_text(text: str) -> tuple[ResumeProfile, str, list[FieldEvidence]]:
    extractor = get_resume_extractor()
    parsed = await extractor.parse(text)
    return parsed, extractor.name, build_evidence(parsed, text, extractor.name)


@app.post("/api/resumes", response_model=ResumeRecord, status_code=201)
async def upload_resume(file: UploadFile = File(...)) -> ResumeRecord:
    original_name = Path(file.filename or "resume").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES: raise HTTPException(415, "仅支持 PDF、DOCX 和 TXT")
    if file.content_type and file.content_type not in CONTENT_TYPES[suffix]: raise HTTPException(415, "文件内容类型与扩展名不匹配")
    content = await file.read(MAX_FILE_SIZE + 1)
    if not content: raise HTTPException(422, "文件为空")
    if len(content) > MAX_FILE_SIZE: raise HTTPException(413, "文件不能超过 10MB")
    digest = hashlib.sha256(content).hexdigest()
    duplicate = find_by_hash(digest)
    if duplicate:
        message = "这份简历已保留，可在资料库点击重新解析" if duplicate.status == "failed" else "这份简历已经上传"
        raise HTTPException(409, {"message": message, "resume_id": duplicate.id})

    resume_id = str(uuid4()); stored_name = f"{resume_id}{suffix}"; path = UPLOAD_DIR / stored_name
    path.write_bytes(content)
    try:
        text = extract_text(path)
        if not text.strip(): raise ValueError("没有提取到文本，扫描版简历需要 OCR")
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, f"简历文本提取失败：{exc}") from exc

    parser = get_resume_extractor().name
    create_pending_resume(resume_id, original_name, stored_name, Path(original_name).stem, parser,
                          detect_language(text), len(content), digest, text)
    try:
        parsed, parser, evidence = await _parse_text(text)
        record = replace_parse_result(resume_id, parsed, parser, evidence, text)
        merge_into_profile(parsed, resume_id)
        return record  # type: ignore[return-value]
    except Exception as exc:
        public_error = _retryable_parse_error(exc, resume_id, "简历解析失败")
        detail = public_error.detail if isinstance(public_error.detail, dict) else {"message": str(public_error.detail)}
        mark_resume_failed(resume_id, str(detail.get("message", "解析失败")))
        raise public_error from exc


@app.patch("/api/resumes/{resume_id}", response_model=ResumeRecord)
def save_resume_route(resume_id: str, payload: ResumeUpdate) -> ResumeRecord:
    changes = payload.model_dump(exclude={"profile"}, exclude_none=True)
    if payload.profile is not None:
        changes["profile"] = payload.profile
    record = update_resume(resume_id, **changes)
    if not record: raise HTTPException(404, "简历不存在")
    return record


@app.get("/api/resumes/{resume_id}/fact-targets", response_model=FactTargets)
def resume_fact_targets(resume_id: str) -> FactTargets:
    try:
        return get_fact_targets(resume_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/resumes/{resume_id}/confirmed-fact", response_model=ResumeRecord)
def confirm_resume_fact(resume_id: str, payload: ConfirmedFactRequest) -> ResumeRecord:
    try:
        return save_confirmed_fact(resume_id, payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FactRevisionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/resumes/{resume_id}", status_code=204, response_class=Response)
def remove_resume(resume_id: str) -> Response:
    stored_name = delete_resume(resume_id)
    if stored_name is None: raise HTTPException(404, "简历不存在")
    if stored_name: (UPLOAD_DIR / Path(stored_name).name).unlink(missing_ok=True)
    return Response(status_code=204)


@app.get("/api/resumes/{resume_id}/download")
def download_resume(resume_id: str) -> FileResponse:
    row = get_resume_internal(resume_id)
    if not row: raise HTTPException(404, "简历不存在")
    path = UPLOAD_DIR / Path(row["stored_filename"]).name
    if not path.exists(): raise HTTPException(404, "原始文件不存在")
    return FileResponse(path, filename=row["filename"])


@app.get("/api/resumes/{resume_id}/preview")
def preview_resume(resume_id: str) -> Response:
    row = get_resume_internal(resume_id)
    if not row: raise HTTPException(404, "简历不存在")
    path = UPLOAD_DIR / Path(row["stored_filename"]).name
    if not path.exists(): raise HTTPException(404, "原始文件不存在")
    if path.suffix.lower() == ".pdf":
        return FileResponse(path, filename=row["filename"], media_type="application/pdf",
                            content_disposition_type="inline")
    return HTMLResponse(preview_html(path, row["filename"]), headers={"X-Content-Type-Options": "nosniff"})


@app.post("/api/resumes/{resume_id}/parse", response_model=ResumeRecord)
async def reparse_resume(resume_id: str) -> ResumeRecord:
    row = get_resume_internal(resume_id)
    if not row: raise HTTPException(404, "简历不存在")
    path = UPLOAD_DIR / Path(row["stored_filename"]).name
    if not path.exists(): raise HTTPException(404, "原始文件不存在")
    mark_resume_parsing(resume_id)
    try:
        text = extract_text(path)
        if not text.strip(): raise ValueError("没有提取到文本，扫描版简历需要 OCR")
        parsed, parser, evidence = await _parse_text(text)
        record = replace_parse_result(resume_id, parsed, parser, evidence, text)
        merge_into_profile(parsed, resume_id)
        return record  # type: ignore[return-value]
    except Exception as exc:
        public_error = _retryable_parse_error(exc, resume_id, "重新解析失败")
        detail = public_error.detail if isinstance(public_error.detail, dict) else {"message": str(public_error.detail)}
        mark_resume_failed(resume_id, str(detail.get("message", "重新解析失败")))
        raise public_error from exc


@app.get("/api/resumes/{resume_id}/text")
def resume_text(resume_id: str) -> dict[str, str]:
    row = get_resume_internal(resume_id)
    if not row: raise HTTPException(404, "简历不存在")
    path = UPLOAD_DIR / Path(row["stored_filename"]).name
    if path.exists():
        try:
            # Re-extract locally so older uploads immediately benefit from extractor fixes
            # without spending another model request.
            return {"text": extract_text(path)}
        except Exception:
            pass
    return {"text": row["raw_text"]}


@app.patch("/api/resumes/{resume_id}/evidence/{evidence_id}", response_model=ResumeRecord)
def review_evidence(resume_id: str, evidence_id: str, payload: ReviewUpdate) -> ResumeRecord:
    current = get_resume(resume_id)
    evidence = next((item for item in current.evidence if item.id == evidence_id), None) if current else None
    if not evidence: raise HTTPException(404, "简历或识别字段不存在")
    if payload.status == "edited":
        if payload.value is None: raise HTTPException(422, "修改后的字段不能为空")
        try:
            sync_edited_profile_value(resume_id, evidence.field_path, payload.value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"字段格式不正确：{exc}") from exc
    record = update_evidence(resume_id, evidence_id, payload.status, payload.value)
    if not record: raise HTTPException(404, "简历或识别字段不存在")
    return record


@app.get("/api/conflicts", response_model=list[ProfileConflict])
def conflicts() -> list[ProfileConflict]: return list_conflicts()


@app.post("/api/conflicts/{conflict_id}/resolve", response_model=ProfileConflict)
def solve_conflict(conflict_id: str, payload: ConflictResolution) -> ProfileConflict:
    conflict = get_conflict(conflict_id)
    if not conflict: raise HTTPException(404, "冲突不存在")
    value = conflict.current_value if payload.choice == "current" else conflict.incoming_value
    if payload.choice == "custom":
        if payload.custom_value is None: raise HTTPException(422, "自定义结果不能为空")
        value = payload.custom_value
    apply_profile_value(conflict.field_path, value)
    resolved = resolve_conflict(conflict_id, value)
    return resolved  # type: ignore[return-value]


@app.get("/api/export", response_model=ExportBundle)
def export_data() -> ExportBundle:
    return ExportBundle(exported_at=datetime.now(timezone.utc), profile=get_profile(),
                        resumes=list_resumes(), conflicts=list_conflicts(pending_only=False))


@app.get("/api/browser/current")
async def current_browser_session() -> dict[str, Any]:
    session_id = browser_demo.session_id
    owns_session = bool(session_id and browser_session_owners.get(session_id) == current_user_id())
    # Recover task UI on refresh without exposing another user's URL or target.
    return {"session_id": session_id if owns_session else None, "occupied": bool(session_id),
            "assistance_version": 1,
            "record_completion_version": 1,
            "journey_version": 1,
            "resume_id": browser_task_resumes.get(session_id, "") if owns_session else ""}


@app.put("/api/browser/{session_id}/resume")
async def select_task_resume(session_id: str, payload: TaskResumeUpdate) -> dict:
    _require_browser_owner(session_id)
    resume = get_resume(payload.resume_id) if payload.resume_id else None
    if payload.resume_id and not resume:
        raise HTTPException(404, "选择的简历不存在")
    try:
        compose_task_profile(get_profile(), resume)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    browser_task_resumes[session_id] = payload.resume_id
    browser_task_epochs[session_id] = str(uuid4())
    browser_image_consents.pop(session_id, None)
    return {"resume_id": payload.resume_id, "context_token": _task_context(session_id)[1]}


@app.post("/api/browser/start", response_model=BrowserSnapshot)
async def start_browser(payload: BrowserStart, request: Request) -> BrowserSnapshot:
    try:
        if configured_browser_engine() == "safari":
            host = request.client.host if request.client else ""
            try:
                local = host == "testclient" or ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise HTTPException(403, "Safari 使用本机浏览器登录状态，只允许本机使用。多用户部署请设置 APP_BROWSER_ENGINE=chromium，以隔离各用户招聘账号。")
        if browser_demo.session_id:
            # A new chat task must not silently close another task or user's
            # browser, including their in-progress application and login.
            raise HTTPException(409, "已有投递浏览器会话正在进行，请先结束当前会话，再打开新的任务")
        resume = get_resume(payload.resume_id) if payload.resume_id else None
        if payload.resume_id and not resume:
            raise HTTPException(404, "选择的简历不存在")
        compose_task_profile(get_profile(), resume)
        if configured_browser_engine() == "safari":
            browser_demo._validate_url(payload.url)
            window = await safari_windows.for_connection(current_user_id(), payload.url, payload.safari_window_token)
            result = await browser_demo.start(payload.url, current_user_id(), target=payload.target, safari_page=window.page)
        else:
            if payload.safari_window_token:
                raise ValueError("当前后端不是 Safari 模式，不能连接 Safari 窗口")
            result = await browser_demo.start(payload.url, current_user_id(), target=payload.target)
        browser_task_resumes.clear()
        browser_task_resumes[result.session_id] = payload.resume_id
        browser_task_epochs.clear()
        browser_task_epochs[result.session_id] = str(uuid4())
        browser_session_owners.clear()
        browser_session_owners[result.session_id] = current_user_id()
        return result
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"自动填写连接失败：{exc}") from exc


def _require_local_browser_request(request: Request) -> None:
    """Local authenticated frontend gesture, not an arbitrary web origin."""
    host = request.client.host if request.client else ""
    try:
        local = host == "testclient" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if not local:
        raise HTTPException(403, "打开本机浏览器只允许本机使用")
    origin = request.headers.get("origin")
    if origin not in {"http://127.0.0.1:5173", "http://localhost:5173"}:
        raise HTTPException(403, "请从本机职达前端主动点击打开浏览器")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不能从其他网站启动本机浏览器")


@app.post("/api/websites/safari/preview-existing")
async def preview_existing_safari(payload: ExistingSafariPreview, request: Request):
    _require_local_browser_request(request)
    if configured_browser_engine() != 'safari' or browser_demo.session_id:
        raise HTTPException(409, '请先结束现有会话，且后端需处于 Safari 模式')
    try:
        browser_demo._validate_url(payload.url)
        # This is an explicit UI handoff, not an automatic front-window attach.
        # A short fixed interval lets the user bring the stated target forward.
        await asyncio.sleep(10)
        if browser_demo.session_id:
            raise HTTPException(409, '已有投递会话，未另行连接窗口')
        token, url = await safari_windows.preview_existing(current_user_id(), payload.url)
        return {'preview_token': token, 'url': url, 'expires_in_seconds': 120}
    except HTTPException:
        raise
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/websites/safari/confirm-existing")
async def confirm_existing_safari(payload: ExistingSafariConfirmation, request: Request):
    _require_local_browser_request(request)
    if configured_browser_engine() != 'safari' or browser_demo.session_id:
        raise HTTPException(409, '已有投递会话或后端不是 Safari 模式，未连接窗口')
    try:
        browser_demo._validate_url(payload.url)
        entry = await safari_windows.confirm_existing(current_user_id(), payload.url, payload.preview_token)
        return {'status': 'requested', 'browser': 'safari', 'automation_connected': False,
                'safari_window_token': entry.token, 'window_reused': True}
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/websites/open")
async def open_normal_website(payload: ExternalWebsiteOpen, request: Request):
    """Only an authenticated local user may launch apps on this computer."""
    _require_local_browser_request(request)
    try:
        if payload.browser == "safari":
            if browser_demo.session_id:
                raise HTTPException(409, "已有自动填写会话，请在当前招聘窗口继续，或先结束该会话")
            entry, reused = await safari_windows.open(current_user_id(), payload.url)
            return {"status": "requested", "browser": "safari", "automation_connected": False,
                    "safari_window_token": entry.token, "window_reused": reused}
        return await open_external_website(payload)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        # Transport errors are fixed sanitized diagnostics, not raw stderr.
        # Ownership failures and timeouts are not necessarily missing consent.
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "本机浏览器打开未完成。Safari 需要 macOS 允许职达启动程序控制 Safari；"
                                 "可先复制网址手动打开，但手动窗口不会被自动接管。") from exc


@app.get("/api/browser/{session_id}/snapshot", response_model=BrowserSnapshot)
async def browser_snapshot(session_id: str) -> BrowserSnapshot:
    try:
        _require_browser_owner(session_id)
        return await browser_demo.snapshot_for(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/browser/{session_id}/expand", response_model=BrowserSnapshot)
async def expand_browser_section(session_id: str, payload: ExpandSectionRequest) -> BrowserSnapshot:
    try:
        _require_browser_owner(session_id)
        return await browser_demo.expand_section(session_id, payload.selector, payload.candidate_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/browser/{session_id}/recognition-diagnostics")
async def browser_recognition_diagnostics(session_id: str) -> dict:
    """Owner-only structural diagnosis; no input values, credentials or model."""
    _require_browser_owner(session_id)
    try:
        return await browser_demo.recognition_diagnostics(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/browser/{session_id}/observation-consent")
async def observation_consent(session_id: str) -> dict:
    _require_browser_owner(session_id)
    revision = _task_context(session_id)[1]
    return {"enabled": browser_image_consents.get(session_id) == revision, "context_token": revision}


@app.post("/api/browser/{session_id}/extraction-audit", response_model=ExtractionAuditResult)
async def extraction_audit(session_id: str, payload: ExtractionAuditRequest) -> ExtractionAuditResult:
    """Read/model-review the entire collected document. Never compile or execute a fill plan."""
    _require_browser_owner(session_id)
    revision = _task_context(session_id)[1]
    if payload.context_token != revision:
        raise HTTPException(409, "页面或资料已变化，请重新同步只读检查")
    if payload.include_images and browser_image_consents.get(session_id) != revision:
        raise HTTPException(409, "本轮题目截图尚未授权，未发送图片")
    if payload.include_images and not payload.use_model:
        raise HTTPException(422, "仅本地检查请关闭模型截图，未采集图片")
    snapshot = await browser_demo.snapshot_for(session_id)
    baseline = document_fingerprint(snapshot)
    observation_workflow = await browser_demo.workflow_state(session_id) if payload.inspect_controls else None
    observation_snapshot = snapshot.model_copy(deep=True)

    def guard():
        _require_browser_owner(session_id)
        if revision != _task_context(session_id)[1]:
            raise ValueError("只读检查期间页面或资料变化，已丢弃结果")

    async def controls(selectors):
        guard()
        result = await browser_demo.observe_form_controls(session_id, selectors,
            expected_snapshot=observation_snapshot, expected_workflow=observation_workflow)
        guard()
        return result

    async def region(selector):
        guard()
        result = await browser_demo.observe_page_region(session_id, selector, include_image=True,
                                                        bring_into_view=True, audit_only=True)
        guard()
        return result

    try:
        result = await audit_extraction(snapshot, payload, observe_controls=controls, observe_region=region)
        guard()
        after = await browser_demo.snapshot_for(session_id)
        if baseline != document_fingerprint(after):
            raise ValueError("检查期间网页题目、经历归属或填写值变化；未沿用旧结果，请只读同步后重试")
        return result
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/browser/{session_id}/observation-consent")
async def set_observation_consent(session_id: str, payload: ObservationConsentRequest) -> dict:
    _require_browser_owner(session_id)
    revision = _task_context(session_id)[1]
    if payload.enabled:
        if payload.context_token != revision:
            raise HTTPException(409, '页面或简历资料已变化，请重新核对截图授权')
        browser_image_consents[session_id] = revision
    else:
        browser_image_consents.pop(session_id, None)
    return {"enabled": payload.enabled, "context_token": revision}


@app.post("/api/browser/{session_id}/observe-region", response_model=PageRegionObservation)
async def observe_browser_region(session_id: str, payload: PageObservationRequest) -> PageRegionObservation:
    """Local preview only: this endpoint does not call or send data to a model."""
    _require_browser_owner(session_id)
    revision = _task_context(session_id)[1]
    if payload.context_token != revision:
        raise HTTPException(409, '页面或简历资料已变化，请重新同步后观察')
    try:
        result = await browser_demo.observe_page_region(session_id, payload.selector,
                                                        include_image=payload.include_image)
        if revision != _task_context(session_id)[1]:
            raise HTTPException(409, "观察期间资料已变化，请重新同步")
        return result
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/browser/{session_id}/field/inspect", response_model=FormReviewResult)
async def inspect_browser_field(session_id: str, payload: ExpandSectionRequest) -> FormReviewResult:
    try:
        _require_browser_owner(session_id)
        snapshot = await browser_demo.inspect_field(session_id, payload.selector)
        plan = _stamp_plan(session_id, create_local_form_plan(snapshot, _task_profile(session_id)))
        return build_form_review(snapshot, plan)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"定位并读取字段失败：{str(exc)[:240]}") from exc


@app.post("/api/browser/{session_id}/native-resume", response_model=NativeResumeImportResult)
async def import_resume_with_site_parser(session_id: str,
                                         payload: NativeResumeImportRequest) -> NativeResumeImportResult:
    try:
        _require_browser_owner(session_id)
        _require_task_resume(session_id, payload.resume_id)
        await _require_application_form(session_id)
        row = get_resume_internal(payload.resume_id)
        if not row:
            raise HTTPException(404, "选择的简历不存在")
        candidate = UPLOAD_DIR / Path(row["stored_filename"]).name
        if not candidate.exists():
            raise HTTPException(404, "选择的简历原始文件不存在")
        return await browser_demo.import_resume_with_site_parser(session_id, candidate, payload.confirm_site_parse)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/browser/{session_id}/workflow", response_model=ApplicationWorkflowState)
async def browser_workflow(session_id: str) -> ApplicationWorkflowState:
    try:
        _require_browser_owner(session_id)
        return await browser_demo.workflow_state(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


async def _require_application_form(session_id: str) -> ApplicationWorkflowState:
    """Do not send profile data into homepage/search/login controls.

    This is a server-side gate, not just a disabled button. A recognition
    failure is a reason to inspect the page, never evidence of completion.
    """
    workflow = await browser_demo.workflow_state(session_id)
    if workflow.navigation_blocker:
        raise HTTPException(409, workflow.navigation_blocker)
    if workflow.stage not in {"profile_form", "application_form", "review"} or workflow.form_fields < 1:
        raise HTTPException(409, "当前尚未确认有效的简历/申请表，请先选择具体岗位并完成必要登录；不会向搜索或筛选控件填写个人资料")
    return workflow


def _agent_checkpoint_status(workflow: ApplicationWorkflowState,
                             requires_user: bool) -> str:
    if workflow.stage == "review" or workflow.final_submit_present:
        return "review"
    return "waiting_user" if requires_user else "active"


def _selected_resume_path(resume_id: str) -> Path | None:
    if not resume_id:
        return None
    row = get_resume_internal(resume_id)
    if not row:
        raise HTTPException(404, "选择的简历不存在")
    candidate = UPLOAD_DIR / Path(row["stored_filename"]).name
    if not candidate.exists():
        raise HTTPException(404, "选择的简历原始文件不存在")
    return candidate


async def _assess_application_agent(session_id: str,
                                    event_action: str = "assess") -> ApplicationAgentTurn:
    _require_browser_owner(session_id)
    snapshot = await browser_demo.snapshot_for(session_id)
    workflow = await browser_demo.workflow_state(session_id)
    check = (await browser_demo.pre_submit_check(session_id)
             if workflow.stage in {"profile_form", "application_form", "review"} else None)
    decision = await decide_application_step(workflow, snapshot, _task_profile(session_id), check)
    saved = save_application_agent_checkpoint(
        session_id, _agent_checkpoint_status(workflow, decision.requires_user),
        workflow.stage, snapshot.url, snapshot.title, event_action, decision.summary,
    )
    return ApplicationAgentTurn(
        decision=decision, workflow=workflow, snapshot=snapshot,
        pre_submit=check, checkpoint=ApplicationAgentCheckpoint.model_validate(saved),
    )


@app.post("/api/browser/{session_id}/agent/assess", response_model=ApplicationAgentTurn)
async def assess_application_agent(session_id: str) -> ApplicationAgentTurn:
    try:
        return await _assess_application_agent(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"流程 Agent 评估失败：{str(exc)[:240]}") from exc


@app.post("/api/browser/{session_id}/agent/step", response_model=ApplicationAgentTurn)
async def run_application_agent_step(session_id: str,
                                     payload: ApplicationAgentStepRequest) -> ApplicationAgentTurn:
    """Execute one bounded, reversible application step and then checkpoint it."""
    try:
        _require_browser_owner(session_id)
        _require_task_resume(session_id, payload.resume_id)
        snapshot = await browser_demo.snapshot_for(session_id)
        workflow = await browser_demo.workflow_state(session_id)
        check = (await browser_demo.pre_submit_check(session_id)
                 if workflow.stage in {"profile_form", "application_form", "review"} else None)
        decision = await decide_application_step(workflow, snapshot, _task_profile(session_id), check)
        action = decision.next_action
        review = None
        execution = None
        assisted = None
        action_taken = ""
        navigation_warning = ""

        if decision.can_execute and action in {"start_application", "browse_jobs", "search_jobs", "open_job"}:
            try:
                workflow = await browser_demo.advance_workflow(
                    session_id, WorkflowAdvanceRequest(intent=action, candidate_id=decision.candidate_id)
                )
            except NavigationNoProgressError as exc:
                # A no-op entrance is an audited pause, not a lost session or
                # partially-filled form. Never retry or invent a destination.
                workflow = exc.workflow
                navigation_warning = str(exc)
            action_taken = action
        elif decision.can_execute and action == "analyze_and_fill":
            if not payload.resume_id:
                raise ValueError("请先选择本次投递使用的简历，再开始填写")
            await _require_application_form(session_id)
            assisted = await _run_application_assist(session_id, ApplicationAssistRequest(
                resume_id=payload.resume_id, allow_site_parse=False))
            snapshot, review, check = assisted.snapshot, assisted.review, assisted.pre_submit
            action_taken = action
        elif decision.can_execute and action == "continue_application":
            # Recheck immediately before navigation; the page may have changed
            # since the model saw it. The service also enforces this boundary.
            check = await browser_demo.pre_submit_check(session_id)
            if not check.ready:
                raise ValueError("当前页检查结果已经变化，不能继续到下一页")
            try:
                workflow = await browser_demo.advance_workflow(
                    session_id, WorkflowAdvanceRequest(intent="continue_application")
                )
            except NavigationNoProgressError as exc:
                workflow = exc.workflow
                navigation_warning = str(exc)
            check = None
            action_taken = action
        elif decision.can_execute and action == "refresh":
            workflow = await browser_demo.workflow_state(session_id)
            action_taken = action

        snapshot = await browser_demo.snapshot_for(session_id)
        workflow = await browser_demo.workflow_state(session_id)
        check = (await browser_demo.pre_submit_check(session_id)
                 if workflow.stage in {"profile_form", "application_form", "review"} else None)
        result_summary = navigation_warning or (assisted.message if assisted else (f"已执行：{decision.next_label}" if action_taken
                          else f"等待用户：{decision.next_label}"))
        # Return a decision for the new page, not the stale pre-navigation plan.
        decision = await decide_application_step(workflow, snapshot, _task_profile(session_id), check, use_model=False)
        if review is None and workflow.stage in {"profile_form", "application_form", "review"}:
            # Human-only missing facts must be visible to the product UI even
            # when no automatic action was safe. Do not require a separate
            # analysis click just to discover what the agent needs to ask.
            plan = _stamp_plan(session_id, create_local_form_plan(snapshot, _task_profile(session_id)))
            review = build_form_review(snapshot, plan)
        if assisted and assisted.status != "ready_for_review":
            decision = decision.model_copy(update={
                "summary": assisted.message, "next_action": "stop", "next_label": "查看本轮未完成原因并重新核对",
                "can_execute": False, "requires_user": True,
                "blockers": list(dict.fromkeys([assisted.message, *decision.blockers]))[:12],
            })
        status = _agent_checkpoint_status(workflow, decision.requires_user)
        saved = save_application_agent_checkpoint(
            session_id, status, workflow.stage, snapshot.url, snapshot.title,
            action_taken or action, result_summary,
        )
        return ApplicationAgentTurn(
            decision=decision, workflow=workflow, snapshot=snapshot,
            action_taken=action_taken, review=review, execution=execution,
            assistance=assisted,
            pre_submit=check, checkpoint=ApplicationAgentCheckpoint.model_validate(saved),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"流程 Agent 执行失败：{str(exc)[:240]}") from exc


@app.post("/api/browser/{session_id}/workflow/advance", response_model=ApplicationWorkflowState)
async def advance_browser_workflow(session_id: str,
                                   payload: WorkflowAdvanceRequest) -> ApplicationWorkflowState:
    try:
        _require_browser_owner(session_id)
        return await browser_demo.advance_workflow(session_id, payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"网页操作失败：{str(exc)[:240]}") from exc


@app.post("/api/browser/{session_id}/workflow/request-code", response_model=ApplicationWorkflowState)
async def request_browser_verification(session_id: str,
                                       payload: VerificationRequest) -> ApplicationWorkflowState:
    try:
        _require_browser_owner(session_id)
        current = get_profile()
        return await browser_demo.request_code(session_id, payload, current.phone, current.email)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/browser/{session_id}/workflow/registration", response_model=ApplicationWorkflowState)
async def fill_browser_registration(session_id: str,
                                    payload: RegistrationCredentialsRequest) -> ApplicationWorkflowState:
    try:
        _require_browser_owner(session_id)
        return await browser_demo.fill_registration(session_id, payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/browser/{session_id}/workflow/verification", response_model=ApplicationWorkflowState)
async def enter_browser_verification(session_id: str,
                                     payload: VerificationCodeRequest) -> ApplicationWorkflowState:
    try:
        _require_browser_owner(session_id)
        return await browser_demo.enter_verification(session_id, payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/browser/{session_id}/plan", response_model=FormPlan)
async def plan_form(session_id: str) -> FormPlan:
    try:
        _require_browser_owner(session_id)
        snapshot = await browser_demo.snapshot_for(session_id)
        return await _model_task_plan(session_id, snapshot)
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"模型分析失败：{exc}") from exc


@app.post("/api/browser/{session_id}/review", response_model=FormReviewResult)
async def review_current_form(session_id: str, use_model: bool = False) -> FormReviewResult:
    try:
        _require_browser_owner(session_id)
        snapshot = await browser_demo.snapshot_for(session_id)
        plan = (await _model_task_plan(session_id, snapshot) if use_model
                else create_local_form_plan(snapshot, _task_profile(session_id)))
        _stamp_plan(session_id, plan)
        return build_form_review(snapshot, plan)
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"表单核对失败：{exc}") from exc


@app.post("/api/browser/{session_id}/execute", response_model=ExecutionResult)
async def execute_form_plan(session_id: str, payload: ExecutePlanRequest) -> ExecutionResult:
    try:
        _require_browser_owner(session_id)
        _require_task_resume(session_id, payload.resume_id)
        if session_id in browser_task_resumes and payload.context_token != _task_context(session_id)[1]:
            raise HTTPException(409, "档案、已确认答案或简历版本已变化，请重新分析后再填写")
        await _require_application_form(session_id)
        resume_path: Path | None = None
        resume_filename = ''
        if payload.resume_id and payload.upload_resume:
            row = get_resume_internal(payload.resume_id)
            if not row: raise HTTPException(404, "选择的简历不存在")
            candidate = UPLOAD_DIR / Path(row["stored_filename"]).name
            if not candidate.exists(): raise HTTPException(404, "选择的简历原始文件不存在")
            resume_path = candidate
            # Storage returns sqlite3.Row, not dict (it has no .get method).
            resume_filename = str(row['filename'] or candidate.name)
        return await browser_demo.execute(session_id, payload, resume_path,
                                          **({'resume_filename':resume_filename} if resume_filename else {}))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/browser/{session_id}/autofill", response_model=HybridAutofillResult)
async def autofill_current_form(session_id: str,
                                payload: HybridAutofillRequest) -> HybridAutofillResult:
    """Fill one stage without navigation/uploads; each stage sees current page values.

    The UI can show the rules result immediately, then request model assistance.
    A model-stage failure never rolls back the already completed rules stage.
    The browser middleware serializes both stages with other browser operations.
    """
    try:
        _require_browser_owner(session_id)
        await _require_application_form(session_id)
        snapshot = await browser_demo.snapshot_for(session_id)
        profile = _task_profile(session_id)
        plan = (create_local_form_plan(snapshot, profile) if payload.phase == "rules"
                else await _model_task_plan(session_id, snapshot))
        _stamp_plan(session_id, plan)
        review = build_form_review(snapshot, plan)
        fields = {field.selector: field for field in snapshot.fields}
        actions = [action for action in review.plan.actions
                   if action.action in {"fill", "select", "check"}
                   and action.confidence >= .85
                   and (not action.sensitive or action.user_confirmed)
                   and action.selector in fields
                   and fields[action.selector].field_type not in {
                       "file", "password", "hidden", "section-button", "submit", "button", "reset",
                   }]
        # No resume argument, selector from the caller, click, or navigation is
        # accepted here. Existing executor policy and read-back still apply.
        execution = await browser_demo.execute(
            session_id, ExecutePlanRequest(actions=actions, min_confidence=.85),
        )
        refreshed = await browser_demo.snapshot_for(session_id)
        # Keep the original actions/expected values, not pre-review's skips,
        # so newly verified and previously matching fields both remain matched.
        final_review = build_form_review(refreshed, plan)
        return HybridAutofillResult(phase=payload.phase, review=final_review, execution=execution)
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"分阶段填写失败，已完成的填写不会撤销：{str(exc)[:240]}") from exc


@app.get("/api/browser/{session_id}/check", response_model=PreSubmitCheck)
async def check_form_before_submit(session_id: str) -> PreSubmitCheck:
    try:
        _require_browser_owner(session_id)
        return await browser_demo.pre_submit_check(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


async def _run_application_assist(session_id: str, payload: ApplicationAssistRequest,
                                  run_id: str | None = None) -> ApplicationAssistResult:
    _require_browser_owner(session_id)
    _require_task_resume(session_id, payload.resume_id)
    run_id = run_id or str(uuid4())
    progress, replay = assist_runs.begin(run_id, current_user_id(), session_id, payload)
    if replay:
        return progress.result
    try:
        result = await _prepare_application_assist(session_id, payload,
            on_progress=lambda event: assist_runs.emit(run_id, event),
            cancelled=lambda: progress.cancel_requested)
        assist_runs.finish(run_id, result)
        return result
    except BaseException as exc:
        assist_runs.interrupt(run_id, exc)
        raise


async def _prepare_application_assist(session_id: str, payload: ApplicationAssistRequest,
                                      *, on_progress=None, cancelled=lambda: False) -> ApplicationAssistResult:
    _require_browser_owner(session_id)
    _require_task_resume(session_id, payload.resume_id)
    if browser_task_resumes.get(session_id) != payload.resume_id:
        raise HTTPException(409, "请先为本次任务绑定所选简历，再开始自动补齐")
    await _require_application_form(session_id)
    resume = get_resume(payload.resume_id)
    if not resume:
        raise HTTPException(404, "本次简历不存在")
    profile, revision = _task_context(session_id)

    def guard():
        if cancelled():
            raise ValueError('你已要求暂停本轮填写；已填内容保留，尚未提交')
        _require_browser_owner(session_id)
        _require_task_resume(session_id, payload.resume_id)
        if _task_context(session_id)[1] != revision:
            raise ValueError("任务、档案或已确认答案发生变化，已停止旧计划，请重新核对")

    names = {"education": "教育经历", "internships": "实习/工作经历", "projects": "项目经历",
             "skills": "技能", "languages": "语言", "certificates": "证书", "awards": "奖项"}
    pending = [names[key] for key in names if getattr(resume.profile, key)
               and any(item.field_path == key and item.status not in {"confirmed", "edited"}
                       for item in resume.evidence)]
    return await prepare_application(browser_demo, session_id, payload, profile, guard=guard,
        model_plan=lambda snapshot: _model_task_plan(session_id, snapshot),
        stamp_plan=lambda plan: _stamp_plan(session_id, plan),
        resume_path=_selected_resume_path(payload.resume_id) if payload.allow_site_parse else None,
        pending_sections=pending, on_progress=on_progress)


@app.post("/api/browser/{session_id}/assist", response_model=ApplicationAssistResult)
async def assist_application(session_id: str, payload: ApplicationAssistRequest,
                             run_id: str | None = Query(default=None, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')) -> ApplicationAssistResult:
    try:
        return await _run_application_assist(session_id, payload, run_id)
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "自动补齐暂时中断，已完成的填写保留；请重新核对当前网页") from exc


@app.get('/api/browser/{session_id}/assist/progress/{run_id}', response_model=ApplicationAssistProgress)
async def assist_progress(session_id: str, run_id: str) -> ApplicationAssistProgress:
    _require_browser_owner(session_id)
    try:
        return assist_runs.get(run_id, current_user_id(), session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post('/api/browser/{session_id}/assist/progress/{run_id}/cancel', response_model=ApplicationAssistProgress)
async def cancel_assist(session_id: str, run_id: str) -> ApplicationAssistProgress:
    progress = await assist_progress(session_id, run_id)
    if progress.status == 'running':
        progress.cancel_requested = True
    return progress


@app.post("/api/browser/{session_id}/journey", response_model=ApplicationJourneyResult)
async def continue_application_journey(session_id: str, payload: ApplicationJourneyRequest) -> ApplicationJourneyResult:
    """The same product agent carries on until the next human/safety boundary."""
    try:
        _require_browser_owner(session_id)
        if browser_task_resumes.get(session_id) != payload.resume_id:
            raise HTTPException(409, "请先绑定本次简历，再让职达继续")

        def source_revision():
            _require_browser_owner(session_id)
            _require_task_resume(session_id, payload.resume_id)
            resume = get_resume(payload.resume_id)
            if not resume:
                raise HTTPException(409, "本次简历不存在，请重新选择")
            # Unlike a single-page plan, this identity excludes page URL so
            # approved entry/next-page navigation does not invalidate itself.
            raw = "|".join((resume.model_dump_json(), get_profile().model_dump_json(),
                            browser_task_epochs.get(session_id, "")))
            return hashlib.sha256(raw.encode()).hexdigest()

        revision = source_revision()
        def guard():
            if source_revision() != revision:
                raise ValueError("本轮推进期间资料或任务发生变化，已停止后续步骤，请重新核对")

        return await continue_journey(max_steps=payload.max_steps, guard=guard,
            run_step=lambda: run_application_agent_step(session_id,
                ApplicationAgentStepRequest(resume_id=payload.resume_id)))
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "职达推进暂时中断；已填写内容保留，请重新同步当前网页") from exc


@app.delete("/api/browser/{session_id}", status_code=204, response_class=Response)
async def close_browser(session_id: str) -> Response:
    _require_browser_owner(session_id)
    if browser_demo.session_id != session_id:
        raise HTTPException(404, "浏览器会话不存在")
    await browser_demo.close()
    browser_session_owners.pop(session_id, None)
    browser_task_resumes.pop(session_id, None)
    browser_task_epochs.pop(session_id, None)
    browser_image_consents.pop(session_id, None)
    return Response(status_code=204)
