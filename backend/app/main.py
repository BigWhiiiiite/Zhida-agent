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

from fastapi import FastAPI, File, HTTPException, Request, Response as FastAPIResponse, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
from dotenv import load_dotenv

from .agent import get_resume_extractor
from .auth_models import AuthSession, LoginRequest, RegisterRequest, UserAccount
from .auth_service import SESSION_DAYS, local_user, login, register, token_hash, user_for_token
from .application_agent import decide_application_step
from .application_knowledge import (KnowledgeCreate, KnowledgeRecord, MappingTarget,
                                    delete_knowledge, initialize as initialize_application_knowledge,
                                    list_knowledge, mapping_targets, save_knowledge)
from .application_models import (ApplicationAgentCheckpoint, ApplicationAgentStepRequest,
                                 ApplicationAgentTurn, ApplicationWorkflowState,
                                 RegistrationCredentialsRequest, VerificationCodeRequest,
                                 VerificationRequest, WorkflowAdvanceRequest)
from .browser_models import (BrowserSnapshot, BrowserStart, ExecutePlanRequest, ExecutionResult,
                             ExpandSectionRequest, FormPlan, FormReviewResult,
                             HybridAutofillRequest, HybridAutofillResult,
                             NativeResumeImportRequest, NativeResumeImportResult, PreSubmitCheck)
from .browser_service import browser_demo
from .chat_api import router as chat_router
from .chat_storage import initialize as initialize_chat
from .extractors import extract_text, preview_html
from .form_agent import build_form_review, create_form_plan, create_local_form_plan
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
        if request.url.path == "/api/browser" or request.url.path.startswith("/api/browser/"):
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
def health() -> dict[str, str]: return {"status": "ok", "product": "Zhida"}


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
    for session_id in owned_sessions:
        browser_session_owners.pop(session_id, None)
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
            options=payload.options, source_url=payload.source_url,
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
    changes = {key: value for key, value in payload.model_dump().items() if value is not None}
    record = update_resume(resume_id, **changes)
    if not record: raise HTTPException(404, "简历不存在")
    return record


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
    return {"session_id": session_id if owns_session else None, "occupied": bool(session_id)}


@app.post("/api/browser/start", response_model=BrowserSnapshot)
async def start_browser(payload: BrowserStart) -> BrowserSnapshot:
    try:
        if browser_demo.session_id:
            # A new chat task must not silently close another task or user's
            # browser, including their in-progress application and login.
            raise HTTPException(409, "已有投递浏览器会话正在进行，请先结束当前会话，再打开新的任务")
        result = await browser_demo.start(payload.url, current_user_id(), target=payload.target)
        browser_session_owners.clear()
        browser_session_owners[result.session_id] = current_user_id()
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"打开网站失败：{exc}") from exc


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
        return await browser_demo.expand_section(session_id, payload.selector)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/browser/{session_id}/field/inspect", response_model=FormReviewResult)
async def inspect_browser_field(session_id: str, payload: ExpandSectionRequest) -> FormReviewResult:
    try:
        _require_browser_owner(session_id)
        snapshot = await browser_demo.inspect_field(session_id, payload.selector)
        plan = create_local_form_plan(snapshot, get_profile())
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
        await _require_application_form(session_id)
        row = get_resume_internal(payload.resume_id)
        if not row:
            raise HTTPException(404, "选择的简历不存在")
        candidate = UPLOAD_DIR / Path(row["stored_filename"]).name
        if not candidate.exists():
            raise HTTPException(404, "选择的简历原始文件不存在")
        return await browser_demo.import_resume_with_site_parser(session_id, candidate)
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
    decision = await decide_application_step(workflow, snapshot, get_profile(), check)
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
        snapshot = await browser_demo.snapshot_for(session_id)
        workflow = await browser_demo.workflow_state(session_id)
        check = (await browser_demo.pre_submit_check(session_id)
                 if workflow.stage in {"profile_form", "application_form", "review"} else None)
        decision = await decide_application_step(workflow, snapshot, get_profile(), check)
        action = decision.next_action
        review = None
        execution = None
        action_taken = ""

        if decision.can_execute and action in {"start_application", "browse_jobs", "search_jobs", "open_job"}:
            workflow = await browser_demo.advance_workflow(
                session_id, WorkflowAdvanceRequest(intent=action, candidate_id=decision.candidate_id)
            )
            action_taken = action
        elif decision.can_execute and action == "analyze_and_fill":
            await _require_application_form(session_id)
            snapshot = await browser_demo.snapshot_for(session_id)
            plan = await create_form_plan(snapshot, get_profile())
            review = build_form_review(snapshot, plan)
            execution = await browser_demo.execute(
                session_id,
                ExecutePlanRequest(actions=plan.actions, min_confidence=.85,
                                   resume_id=payload.resume_id),
                _selected_resume_path(payload.resume_id),
            )
            check = execution.pre_submit
            action_taken = action
        elif decision.can_execute and action == "continue_application":
            # Recheck immediately before navigation; the page may have changed
            # since the model saw it. The service also enforces this boundary.
            check = await browser_demo.pre_submit_check(session_id)
            if not check.ready:
                raise ValueError("当前页检查结果已经变化，不能继续到下一页")
            workflow = await browser_demo.advance_workflow(
                session_id, WorkflowAdvanceRequest(intent="continue_application")
            )
            check = None
            action_taken = action
        elif decision.can_execute and action == "refresh":
            workflow = await browser_demo.workflow_state(session_id)
            action_taken = action

        snapshot = await browser_demo.snapshot_for(session_id)
        workflow = await browser_demo.workflow_state(session_id)
        check = (await browser_demo.pre_submit_check(session_id)
                 if workflow.stage in {"profile_form", "application_form", "review"} else None)
        result_summary = (f"已执行：{decision.next_label}" if action_taken
                          else f"等待用户：{decision.next_label}")
        # Return a decision for the new page, not the stale pre-navigation plan.
        decision = await decide_application_step(workflow, snapshot, get_profile(), check, use_model=False)
        status = _agent_checkpoint_status(workflow, decision.requires_user)
        saved = save_application_agent_checkpoint(
            session_id, status, workflow.stage, snapshot.url, snapshot.title,
            action_taken or action, result_summary,
        )
        return ApplicationAgentTurn(
            decision=decision, workflow=workflow, snapshot=snapshot,
            action_taken=action_taken, review=review, execution=execution,
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
        return await create_form_plan(snapshot, get_profile())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"模型分析失败：{exc}") from exc


@app.post("/api/browser/{session_id}/review", response_model=FormReviewResult)
async def review_current_form(session_id: str, use_model: bool = False) -> FormReviewResult:
    try:
        _require_browser_owner(session_id)
        snapshot = await browser_demo.snapshot_for(session_id)
        plan = (await create_form_plan(snapshot, get_profile()) if use_model
                else create_local_form_plan(snapshot, get_profile()))
        return build_form_review(snapshot, plan)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"表单核对失败：{exc}") from exc


@app.post("/api/browser/{session_id}/execute", response_model=ExecutionResult)
async def execute_form_plan(session_id: str, payload: ExecutePlanRequest) -> ExecutionResult:
    try:
        _require_browser_owner(session_id)
        await _require_application_form(session_id)
        resume_path: Path | None = None
        if payload.resume_id:
            row = get_resume_internal(payload.resume_id)
            if not row: raise HTTPException(404, "选择的简历不存在")
            candidate = UPLOAD_DIR / Path(row["stored_filename"]).name
            if not candidate.exists(): raise HTTPException(404, "选择的简历原始文件不存在")
            resume_path = candidate
        return await browser_demo.execute(session_id, payload, resume_path)
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
        profile = get_profile()
        plan = (create_local_form_plan(snapshot, profile) if payload.phase == "rules"
                else await create_form_plan(snapshot, profile))
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


@app.delete("/api/browser/{session_id}", status_code=204, response_class=Response)
async def close_browser(session_id: str) -> Response:
    _require_browser_owner(session_id)
    if browser_demo.session_id != session_id:
        raise HTTPException(404, "浏览器会话不存在")
    await browser_demo.close()
    browser_session_owners.pop(session_id, None)
    return Response(status_code=204)
