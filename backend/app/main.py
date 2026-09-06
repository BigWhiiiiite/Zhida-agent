from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .agent import get_resume_extractor
from .extractors import extract_text
from .models import ResumeRecord, ResumeUpdate
from .storage import create_resume, get_resume, initialize, list_resumes, update_resume


ROOT = Path(__file__).resolve().parents[1]
UPLOAD_DIR = ROOT / "uploads"
ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt"}
MAX_FILE_SIZE = 10 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="OfferPilot API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/resumes", response_model=list[ResumeRecord])
def resumes() -> list[ResumeRecord]:
    return list_resumes()


@app.get("/api/resumes/{resume_id}", response_model=ResumeRecord)
def resume(resume_id: str) -> ResumeRecord:
    record = get_resume(resume_id)
    if not record:
        raise HTTPException(404, "简历不存在")
    return record


@app.post("/api/resumes", response_model=ResumeRecord, status_code=201)
async def upload_resume(file: UploadFile = File(...)) -> ResumeRecord:
    original_name = Path(file.filename or "resume").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(415, "仅支持 PDF、DOCX 和 TXT")
    content = await file.read(MAX_FILE_SIZE + 1)
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, "文件不能超过 10MB")

    resume_id = str(uuid4())
    path = UPLOAD_DIR / f"{resume_id}{suffix}"
    path.write_bytes(content)
    try:
        text = extract_text(path)
        if not text.strip():
            raise ValueError("没有提取到文本，扫描版简历需要 OCR")
        extractor = get_resume_extractor()
        profile = await extractor.parse(text)
        label = profile.name or Path(original_name).stem
        return create_resume(resume_id, original_name, label, profile, extractor.name)
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, f"简历解析失败：{exc}") from exc


@app.patch("/api/resumes/{resume_id}", response_model=ResumeRecord)
def save_resume(resume_id: str, payload: ResumeUpdate) -> ResumeRecord:
    current = get_resume(resume_id)
    if not current:
        raise HTTPException(404, "简历不存在")
    record = update_resume(
        resume_id,
        payload.label if payload.label is not None else current.label,
        payload.profile if payload.profile is not None else current.profile,
    )
    return record  # type: ignore[return-value]
