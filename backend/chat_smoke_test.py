"""Offline chat-intake tests; no model service, browser or personal records."""
from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import ModelSettings
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image

from app import chat_service, chat_storage, storage
from app.chat_api import InMemoryChatParser, router
from app.chat_models import ChatImageLinkProposal, ChatInterpretation, ChatLink, ChatTaskDraft


def image_bytes(mode="PNG", *, exif=False):
    image = Image.new("RGB", (120, 80), "white")
    output = io.BytesIO()
    options = {}
    if exif:
        metadata = Image.Exif()
        metadata[270] = "private-metadata-must-not-survive"
        options["exif"] = metadata
    image.save(output, format=mode, **options)
    return output.getvalue()


def test_urls() -> None:
    for url in (
        "http://localhost/form", "http://127.0.0.1/form", "http://[::1]/form", "https://8.8.8.8/job",
        "http://10.0.0.1/", "http://169.254.169.254/", "http://127.1/", "http://2130706433/",
        "http://0x7f000001/", "http://0x7f.0.0.1/", "https://jobs.local/", "https://jobs.internal/form",
        "file:///etc/passwd", "javascript:alert(1)", "https://user:password@example.com/",
        "https://example.com:8000/", "https://localhost./", "https://example.com\\@127.0.0.1/",
        "https://%31%32%37.0.0.1/", "https://example.com/\njob",
    ):
        try:
            chat_service.safe_task_url(url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe URL accepted: {url}")
    assert chat_service.safe_task_url("https://jobs.example.com/form?id=1#/job/2") == "https://jobs.example.com/form?id=1#/job/2"
    links, blocked = chat_service.extract_links("岗位 https://jobs.example.com/1，另一个 http://127.0.0.1/。")
    assert len(links) == 1 and blocked == 1 and not links[0].verified


def test_images() -> None:
    prepared = chat_service.prepare_image(image_bytes("JPEG", exif=True), "image/jpeg", "../../photo.jpg")
    assert prepared.name == "photo.jpg" and prepared.mime_type == "image/jpeg"
    with Image.open(io.BytesIO(prepared.content)) as clean:
        assert not clean.getexif() and not clean.info.get("exif")
    assert b"private-metadata-must-not-survive" not in prepared.content
    for data, mime in ((b"<svg><script/></svg>", "image/svg+xml"), (b"not-an-image", "image/png"),
                       (image_bytes(), "image/jpeg"), (b"x" * (chat_service.MAX_IMAGE_BYTES + 1), "image/png")):
        try:
            chat_service.prepare_image(data, mime, "invalid")
        except ValueError:
            pass
        else:
            raise AssertionError(f"bad image accepted: {mime}")
    # Decode an actual locally generated QR rather than trusting model output.
    import numpy as np
    import zxingcpp

    qr_url = "https://careers.example.com/jobs/qr-fixture"
    bitmap = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, qr_url, 360, 360)
    image = Image.fromarray(np.asarray(bitmap))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    qr = chat_service.prepare_image(buffer.getvalue(), "image/png", "recruitment-qr.png")
    assert qr.qr_available and qr_url in qr.qr_texts
    return qr


async def test_model_input(prepared) -> None:
    fake = SimpleNamespace(final_output=ChatInterpretation(company="示例公司"))
    run = AsyncMock(return_value=fake)
    draft = ChatTaskDraft(company="示例公司", links=[ChatLink(url="https://example.com/" + "x" * 1500, source="text") for _ in range(12)])
    with patch.object(chat_service, "configured_model", return_value=("fixture-model", ModelSettings())), \
            patch.object(chat_service.Runner, "run", run):
        await chat_service._interpret("文字" * 6000, prepared, draft, draft.links)
    args = run.call_args.args
    assert args[0].tools == [] and args[0].model_settings.store is not True
    contents = args[1][0]["content"]
    assert contents[0]["type"] == "input_text" and len(contents[0]["text"]) <= chat_service.MAX_MODEL_CONTEXT_CHARS
    assert contents[1]["type"] == "input_image" and contents[1]["image_url"].startswith("data:image/jpeg;base64,")
    sent = base64.b64decode(contents[1]["image_url"].split(",", 1)[1])
    assert sent == prepared.content


def test_api(prepared) -> None:
    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        token = storage.set_current_user(request.headers.get("x-test-user", "test-a"))
        try:
            return await call_next(request)
        finally:
            storage.reset_current_user(token)

    def send(client, conversation_id, text="", *, consent=False, image=None, headers=None):
        parts = {"text": (None, text), "model_consent": (None, "true" if consent else "false")}
        if image:
            parts["image"] = image
        return client.post(f"/api/chat/conversations/{conversation_id}/messages", files=parts, headers=headers or {})

    with TestClient(app) as client:
        assert client.post("/api/chat/conversations", headers={"Origin": "https://evil.example.com"}).status_code == 403
        created = client.post("/api/chat/conversations", json={})
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]
        assert created.json()["messages"] == [] and created.json()["draft"] is None
        assert client.get("/api/chat/conversations").json()[0]["id"] == conversation_id
        assert client.get(f"/api/chat/conversations/{conversation_id}", headers={"x-test-user": "test-b"}).status_code == 404
        assert client.get("/api/chat/conversations", headers={"x-test-user": "test-b"}).json() == []
        assert send(client, conversation_id, "hi", headers={"x-test-user": "test-b"}).status_code == 404
        assert send(client, conversation_id, "hi", headers={"Origin": "null"}).status_code == 403

        with patch.object(chat_service, "configured_model", side_effect=AssertionError("no consent means no remote call")):
            response = send(client, conversation_id,
                "申请这个岗位\n公司：示例科技\n岗位：Agent工程师\n城市：北京\n届别：2027校招\nhttps://careers.example.com/job/1")
        assert response.status_code == 200, response.text
        draft = response.json()["draft"]
        assert draft["company"] == "示例科技" and draft["city"] == "北京" and draft["intent"] == "apply"
        assert draft["url"] == "https://careers.example.com/job/1" and draft["needs_confirmation"]
        assert draft["links"][0]["source"] == "text" and not draft["links"][0]["verified"]
        assert len(response.json()["messages"]) == 2
        response = send(client, conversation_id, "把城市改成上海")
        assert response.json()["draft"]["city"] == "上海"
        assert response.json()["draft"]["job_title"] == "Agent工程师"
        response = send(client, conversation_id, "公司改为另一家示例公司")
        assert response.json()["draft"]["company"] == "另一家示例公司"
        assert response.json()["draft"]["url"] == response.json()["draft"]["job_title"] == ""

        with patch.object(chat_service, "_interpret", AsyncMock(side_effect=RuntimeError("sk-private-secret upstream hostname"))):
            response = send(client, conversation_id, "帮我识别", consent=True)
        assert response.status_code == 200 and "sk-private-secret" not in response.text
        assert response.json()["messages"][-2]["content"] == "帮我识别"
        assert any("模型本次未能" in item for item in response.json()["draft"]["warnings"])

        imaginary = ChatInterpretation(company="示例科技", job_title="Agent工程师", city="北京", intent="apply",
            selected_url="https://invented.example.com/unknown", image_links=[ChatImageLinkProposal(
                url="https://invented.example.com/unknown", visible_text="https://invented.example.com/unknown")])
        with patch.object(chat_service, "_interpret", AsyncMock(return_value=imaginary)):
            response = send(client, conversation_id, "请识别这句话", consent=True)
        assert response.status_code == 200 and response.json()["draft"]["url"] == ""
        assert not response.json()["draft"]["links"], "no image means model image links have no source"

        with patch.object(chat_service, "_interpret", AsyncMock(return_value=imaginary)):
            response = send(client, conversation_id, "识别招聘图", consent=True,
                            image=("../../poster.png", image_bytes(), "image/png"))
        assert response.status_code == 200, response.text
        assert response.json()["messages"][-2]["image_names"] == ["poster.png"]
        assert response.json()["draft"]["url"] == ""
        assert response.json()["draft"]["links"][0]["source"] == "image"
        assert not response.json()["draft"]["links"][0]["verified"]
        assert "data:image" not in response.text
        ambiguous = imaginary.model_copy(update={"candidate_job_titles": ["Agent工程师", "测试开发工程师"]})
        with patch.object(chat_service, "_interpret", AsyncMock(return_value=ambiguous)):
            response = send(client, conversation_id, "图里多个岗位", consent=True,
                            image=("poster.png", image_bytes(), "image/png"))
        assert response.json()["draft"]["intent"] == "clarify" and not response.json()["draft"]["job_title"]

        response = send(client, conversation_id, "https://one.example.com/1 https://two.example.com/2")
        assert response.json()["draft"]["url"] == "" and len(response.json()["draft"]["links"]) >= 2
        response = send(client, conversation_id, "http://127.0.0.1/ http://10.0.0.1/")
        assert response.status_code == 200 and not response.json()["draft"]["url"]
        assert all("127.0.0.1" not in item["url"] for item in response.json()["draft"]["links"])

        assert send(client, conversation_id, image=("bad.svg", b"<svg/>", "image/svg+xml")).status_code == 422
        assert send(client, conversation_id, image=("bad.png", b"broken-image", "image/png")).status_code == 422
        assert send(client, conversation_id, image=("huge.png", b"x" * (chat_service.MAX_IMAGE_BYTES + 1), "image/png")).status_code == 413
        assert send(client, conversation_id, "x" * (chat_service.MAX_TEXT_LENGTH + 1)).status_code == 422
        assert send(client, conversation_id).status_code == 422
        assert client.post(f"/api/chat/conversations/{conversation_id}/messages", data={"text": "wrong-encoding"}).status_code == 415
        duplicate = [("text", (None, "hi")), ("image", ("a.png", image_bytes(), "image/png")),
                     ("image", ("b.png", image_bytes(), "image/png"))]
        assert client.post(f"/api/chat/conversations/{conversation_id}/messages", files=duplicate).status_code == 400

        # A valid >1MB image still stays in the in-memory upload spool.
        seen = []
        original_parse = InMemoryChatParser.parse

        async def inspected_parse(self):
            form = await original_parse(self)
            seen.extend(value.file._rolled for _, value in form.multi_items() if hasattr(value, "file"))
            return form

        padded = image_bytes() + b"\0" * (2 * 1024 * 1024)
        with patch.object(InMemoryChatParser, "parse", inspected_parse):
            response = send(client, conversation_id, image=("large.png", padded, "image/png"))
        assert response.status_code == 200 and seen == [False], (response.text, seen)
        with storage._connection() as conn:
            json_records = "".join(row[0] for row in conn.execute("SELECT conversation_json FROM chat_conversations"))
            assert "data:image" not in json_records and "base64," not in json_records

        assert len(client.get(f"/api/chat/conversations/{conversation_id}").json()["messages"]) >= 20
        edit_url = f"/api/chat/conversations/{conversation_id}/draft"
        assert client.patch(edit_url, json={"company": "示例更正"}, headers={"x-test-user": "test-b"}).status_code == 404
        assert client.patch(edit_url, json={"company": "示例更正"}, headers={"Origin": "https://evil.example.com"}).status_code == 403
        assert client.patch(edit_url, json={"verified": True}).status_code == 422
        assert client.patch(edit_url, json={"url": "http://localhost/form"}).status_code == 422
        assert client.patch(edit_url, json={"city": "x" * 101}).status_code == 422
        assert client.patch(edit_url, json={"job_title": "x" * 201}).status_code == 422
        manual = client.patch(edit_url, json={"company": "人工核对示例公司", "job_title": "人工核对Agent岗位",
            "city": "北京", "recruitment_cycle": "2027校招", "url": "https://careers.example.com/corrected", "intent": "apply"})
        assert manual.status_code == 200, manual.text
        assert manual.json()["draft"]["url"] == "https://careers.example.com/corrected"
        assert manual.json()["draft"]["links"][0]["source"] == "text"
        assert manual.json()["draft"]["needs_confirmation"] and not manual.json()["draft"]["links"][0]["verified"]
        observed = []

        async def inspect_previous(text, image, previous, links):
            observed.append(previous.model_dump())
            return ChatInterpretation()

        with patch.object(chat_service, "_interpret", inspect_previous):
            reply = send(client, conversation_id, "继续完善", consent=True)
        assert reply.status_code == 200 and observed[0]["company"] == "人工核对示例公司"
        assert observed[0]["url"] == "https://careers.example.com/corrected"
        cleared = client.patch(edit_url, json={"city": "上海"})
        assert cleared.status_code == 200 and not cleared.json()["draft"]["url"]
        assert cleared.json()["draft"]["company"] == "人工核对示例公司"
        # Manual URL corrections and explicit clears dominate stale model
        # suggestions across turns; neither history nor selected_url may undo them.
        old_url, new_url = "https://careers.example.com/old", "https://careers.example.com/new"
        send(client, conversation_id, old_url)
        manual = client.patch(edit_url, json={"url": new_url})
        assert [item["url"] for item in manual.json()["draft"]["links"]] == [new_url]
        with patch.object(chat_service, "_interpret", AsyncMock(return_value=ChatInterpretation(selected_url=old_url))):
            unchanged = send(client, conversation_id, "继续检查资料", consent=True)
        assert unchanged.json()["draft"]["url"] == new_url
        assert old_url not in [item["url"] for item in unchanged.json()["draft"]["links"]]
        with patch.object(chat_service, "_interpret", AsyncMock(return_value=ChatInterpretation(
                clear_fields=["url"], selected_url=new_url))):
            cleared = send(client, conversation_id, "先移除当前申请入口", consent=True)
        assert cleared.json()["draft"]["url"] == "" and cleared.json()["draft"]["links"] == []
        with patch.object(chat_service, "_interpret", AsyncMock(return_value=ChatInterpretation(selected_url=new_url))):
            remains_clear = send(client, conversation_id, "继续", consent=True)
        assert remains_clear.json()["draft"]["url"] == "" and remains_clear.json()["draft"]["links"] == []
        client.patch(edit_url, json={"url": new_url})
        with patch.object(chat_service, "_interpret", AsyncMock(return_value=ChatInterpretation(selected_url=new_url))):
            local_clear = send(client, conversation_id, "清空链接", consent=True)
        assert not local_clear.json()["draft"]["url"] and not local_clear.json()["draft"]["links"]
        client.patch(edit_url, json={"url": new_url})
        patched_clear = client.patch(edit_url, json={"url": ""})
        assert not patched_clear.json()["draft"]["url"] and not patched_clear.json()["draft"]["links"]
        # An explicitly supplied fresh link remains a supported correction.
        with patch.object(chat_service, "_interpret", AsyncMock(return_value=ChatInterpretation(selected_url=old_url))):
            explicit_new = send(client, conversation_id, old_url, consent=True)
        assert explicit_new.json()["draft"]["url"] == old_url


async def test_qr_and_conflict(prepared) -> None:
    token = storage.set_current_user("test-qr")
    try:
        conversation = chat_storage.create_conversation()
        with patch.object(chat_service, "_interpret", side_effect=AssertionError("QR is local")):
            result = await chat_service.append_message(conversation.id, "这个招聘链接", prepared, False)
        assert result.draft.links[0].source == "qr" and result.draft.url == prepared.qr_texts[0]
        stale = result.model_copy(deep=True)
        previous_time = result.updated_at
        chat_storage.save_turn(result, previous_time)
        try:
            chat_storage.save_turn(stale, previous_time)
        except chat_storage.ConversationConflict:
            pass
        else:
            raise AssertionError("stale conversation overwrote newer message")
    finally:
        storage.reset_current_user(token)


def run():
    original = storage.DATA_DIR, storage.DB_PATH
    with TemporaryDirectory(prefix="zhida-chat-test-") as temp:
        storage.DATA_DIR, storage.DB_PATH = Path(temp), Path(temp) / "chat.db"
        try:
            test_urls()
            prepared = test_images()
            asyncio.run(test_model_input(prepared))
            test_api(prepared)
            asyncio.run(test_qr_and_conflict(prepared))
        finally:
            storage.DATA_DIR, storage.DB_PATH = original
    print("chat smoke passed: tenant isolation, consent, draft corrections, SSRF syntax, images/metadata, real QR, bounded model context, no execution")


if __name__ == "__main__":
    run()
