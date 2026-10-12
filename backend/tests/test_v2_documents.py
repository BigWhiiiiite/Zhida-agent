from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.backend_v2.documents import DocumentStore, extract_document
from app.backend_v2.dispatch import get_handlers
from app.backend_v2.repository import Repository
from app.backend_v2.workers import WorkerDispatcher


def test_document_worker_real_parse_idempotence_and_tampering(v2_db, tmp_path, monkeypatch):
    user = "document-test-" + uuid4().hex
    Repository(v2_db).ensure_user(user)
    monkeypatch.setenv("ZHIDA_V2_FILE_ROOT", str(tmp_path / "store"))
    source = tmp_path / "synthetic.txt"
    source.write_text("合成测试资料\n项目：实现 Agent 工具调用与 RAG 检索。", encoding="utf-8")
    store = DocumentStore(v2_db)
    job = store.ingest(user, source)
    assert store.ingest(user, source) == job
    with v2_db.transaction(user) as cx:
        event = cx.execute(text("SELECT current_event_id FROM zhida.background_jobs WHERE id=:id"),
                           {"id": job["job_id"]}).scalar()
    dispatcher = WorkerDispatcher(v2_db, handlers=get_handlers(v2_db))
    assert dispatcher.dispatch(event_id=event, user_id=user)["status"] == "completed"
    assert dispatcher.dispatch(event_id=event, user_id=user)["status"] == "completed"
    with v2_db.transaction(user) as cx:
        parsed = cx.execute(text("SELECT * FROM zhida.parsed_documents WHERE user_id=:u"), {"u": user}).mappings().one()
        assert parsed["status"] == "needs_review" and "工具调用" in parsed["blocks"][0]["text"]
        assert cx.execute(text("SELECT count(*) FROM zhida.profile_facts WHERE user_id=:u"), {"u": user}).scalar() == 0
        assert cx.execute(text("SELECT count(*) FROM zhida.parsed_documents WHERE user_id=:u"), {"u": user}).scalar() == 1


def test_document_parser_does_not_claim_ocr_or_unlimited_size(tmp_path):
    from pypdf import PdfWriter
    target = tmp_path / "empty.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with target.open("wb") as output:
        writer.write(output)
    result = extract_document(target, "application/pdf")
    assert result["status"] == "needs_ocr" and result["warnings"]
    with pytest.raises(ValueError):
        extract_document(target, "image/jpeg")
