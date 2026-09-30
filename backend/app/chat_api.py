"""Authenticated chat intake API. No side-effectful application routes here."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .chat_models import ChatConversation, ChatConversationSummary, ChatDraftUpdate
from .chat_service import MAX_IMAGE_BYTES, MAX_TEXT_LENGTH, append_message, prepare_image, update_draft
from .chat_storage import (ConversationConflict, ConversationNotFound, create_conversation,
                           get_conversation, list_conversations)


router = APIRouter(prefix="/api/chat", tags=["chat"])
MAX_MULTIPART_BYTES = MAX_IMAGE_BYTES + 128 * 1024
ALLOWED_ORIGINS = {"http://127.0.0.1:5173", "http://localhost:5173"}


def _require_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        raise HTTPException(403, "消息只能从本地职达前端发送")


class InMemoryChatParser(MultiPartParser):
    # Default UploadFile rolls over at 1 MB. This endpoint rejects the entire
    # body before parsing if it could exceed the in-memory spool threshold.
    spool_max_size = MAX_MULTIPART_BYTES + 1


async def _message_form(request: Request):
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise HTTPException(415, "请使用 multipart/form-data 发送文字和图片")
    length = request.headers.get("content-length", "")
    if length:
        try:
            size = int(length)
        except ValueError as exc:
            raise HTTPException(400, "请求长度无效") from exc
        if size < 0 or size > MAX_MULTIPART_BYTES:
            raise HTTPException(413, "消息或图片过大，图片最多 8 MB")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_MULTIPART_BYTES:
            raise HTTPException(413, "消息或图片过大，图片最多 8 MB")
        body.extend(chunk)

    async def stream():
        yield bytes(body)

    try:
        return await InMemoryChatParser(request.headers, stream(), max_files=1, max_fields=3,
                                        max_part_size=64 * 1024).parse()
    except MultiPartException as exc:
        raise HTTPException(400, "表单格式无效；每条消息只支持一张图片和指定文字字段") from exc


@router.get("/conversations", response_model=list[ChatConversationSummary])
def conversations() -> list[ChatConversationSummary]:
    return list_conversations()


@router.post("/conversations", response_model=ChatConversation, status_code=201)
def new_conversation(request: Request) -> ChatConversation:
    _require_origin(request)
    try:
        return create_conversation()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/conversations/{conversation_id}", response_model=ChatConversation)
def conversation(conversation_id: str) -> ChatConversation:
    try:
        return get_conversation(conversation_id)
    except ConversationNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc


@router.patch("/conversations/{conversation_id}/draft", response_model=ChatConversation)
def edit_draft(conversation_id: str, payload: ChatDraftUpdate, request: Request) -> ChatConversation:
    _require_origin(request)
    try:
        return update_draft(conversation_id, payload)
    except ConversationNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc
    except ConversationConflict as exc:
        raise HTTPException(409, "会话有新消息，请刷新后重试") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/conversations/{conversation_id}/messages", response_model=ChatConversation,
    openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {
        "schema": {"type": "object", "properties": {
            "text": {"type": "string", "maxLength": MAX_TEXT_LENGTH},
            "image": {"type": "string", "format": "binary", "description": "PNG/JPEG/WEBP <= 8 MB"},
            "model_consent": {"type": "boolean", "default": False},
        }}}}}})
async def message(conversation_id: str, request: Request) -> ChatConversation:
    _require_origin(request)
    # Check ownership before even decoding an uploaded image or allocating an
    # expensive model request. A foreign and an unknown ID both return 404.
    try:
        get_conversation(conversation_id)
    except ConversationNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc
    form = await _message_form(request)
    try:
        allowed = {"text", "image", "model_consent"}
        if any(key not in allowed or len(form.getlist(key)) != 1 for key in form):
            raise HTTPException(422, "消息字段无效或重复")
        text = form.get("text", "")
        consent = form.get("model_consent", "false")
        upload = form.get("image")
        if not isinstance(text, str) or not isinstance(consent, str):
            raise HTTPException(422, "文字和授权字段格式无效")
        if consent.lower() not in {"true", "false"}:
            raise HTTPException(422, "model_consent 必须为 true 或 false")
        if len(text) > MAX_TEXT_LENGTH:
            raise HTTPException(422, "每条消息最多 12000 字")
        image = None
        if upload is not None:
            if not isinstance(upload, UploadFile):
                raise HTTPException(422, "image 必须为图片文件")
            content = await upload.read(MAX_IMAGE_BYTES + 1)
            if len(content) > MAX_IMAGE_BYTES:
                raise HTTPException(413, "图片不能超过 8 MB")
            image = prepare_image(content, upload.content_type or "", upload.filename or "")
        return await append_message(conversation_id, text, image, consent.lower() == "true")
    except ConversationNotFound as exc:
        raise HTTPException(404, "会话不存在") from exc
    except ConversationConflict as exc:
        raise HTTPException(409, "会话有新消息，请刷新后重试") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await form.close()
