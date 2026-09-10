from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, Response as FastAPIResponse, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from .agent import get_resume_extractor
from .auth_models import AuthSession, LoginRequest, RegisterRequest, UserAccount
from .auth_service import SESSION_DAYS, login, register, token_hash, user_for_token
from .application_models import (ApplicationWorkflowState, VerificationCodeRequest,
                                 VerificationRequest, WorkflowAdvanceRequest)
from .browser_models import BrowserSnapshot, BrowserStart, ExecutePlanRequest, ExecutionResult, FormPlan, PreSubmitCheck
from .browser_service import browser_demo
from .extractors import extract_text, preview_html
from .form_agent import create_form_plan
from .job_models import ApplicationQueueItem, QueueAddRequest, RecommendationBatch
from .job_recommendations import add_to_queue, queue_items, recommendation_batch, remove_from_queue
from .models import (ApplicationAnswerUpdate, CandidateProfile, ConflictResolution, ExportBundle, FieldEvidence,
                     ModelHealth, ProfileConflict, ResumeProfile, ResumeRecord, ResumeUpdate, ReviewUpdate)
from .model_provider import check_model_health
from .profile_service import (apply_profile_value, build_evidence, detect_language, merge_into_profile,
                              save_application_answer, sync_edited_profile_value)
from .storage import (create_pending_resume, current_user_id, delete_resume, delete_session_record,
                      find_by_hash, get_conflict, get_profile,
                      get_resume, get_resume_internal, initialize, list_conflicts, list_resumes,
                      mark_resume_failed, mark_resume_parsing, replace_parse_result, reset_current_user,
                      resolve_conflict, save_profile, set_current_user,
                      update_evidence, update_resume)


ROOT = Path(__file__).resolve().parents[1]
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize(); UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield
    await browser_demo.close()


app = FastAPI(title="职达 Zhida API", description="候选人资料、岗位推荐、简历解析与求职表单 Demo", version="0.4.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def require_account(request: Request, call_next):
    if request.method == "OPTIONS" or not request.url.path.startswith("/api/") or request.url.path in PUBLIC_API_PATHS:
        return await call_next(request)
    user = user_for_token(request.cookies.get(SESSION_COOKIE, ""))
    if not user:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    request.state.user = user
    context_token = set_current_user(user.id)
    try:
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
        return save_application_answer(payload.question, payload.field_name, payload.value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/jobs/recommendations", response_model=RecommendationBatch)
def job_recommendations(location: str = "") -> RecommendationBatch:
    return recommendation_batch(get_profile(), location)


@app.get("/api/jobs/queue", response_model=list[ApplicationQueueItem])
def application_queue() -> list[ApplicationQueueItem]:
    return queue_items(get_profile())


@app.post("/api/jobs/queue", response_model=list[ApplicationQueueItem], status_code=201)
def add_application_queue(payload: QueueAddRequest) -> list[ApplicationQueueItem]:
    try:
        return add_to_queue(get_profile(), payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/jobs/queue/{queue_id}", status_code=204, response_class=Response)
def remove_application_queue(queue_id: str) -> Response:
    if not remove_from_queue(queue_id):
        raise HTTPException(404, "投递清单项目不存在")
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


@app.post("/api/browser/start", response_model=BrowserSnapshot)
async def start_browser(payload: BrowserStart) -> BrowserSnapshot:
    try:
        result = await browser_demo.start(payload.url)
        browser_session_owners.clear()
        browser_session_owners[result.session_id] = current_user_id()
        return result
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


@app.get("/api/browser/{session_id}/workflow", response_model=ApplicationWorkflowState)
async def browser_workflow(session_id: str) -> ApplicationWorkflowState:
    try:
        _require_browser_owner(session_id)
        return await browser_demo.workflow_state(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


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


@app.post("/api/browser/{session_id}/execute", response_model=ExecutionResult)
async def execute_form_plan(session_id: str, payload: ExecutePlanRequest) -> ExecutionResult:
    try:
        _require_browser_owner(session_id)
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
