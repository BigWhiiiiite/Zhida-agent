"""Local workbench preview of the D2 normalizer, not a workflow or fill run.

Only the bound BrowserRunner supplies evidence. Requests cannot upload a DOM,
document identity or selectors. No profile/model/DB client is used here. The
caller installs this router behind authenticated browser-operation serialization.
"""
from __future__ import annotations

from copy import deepcopy
from time import monotonic
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .backend_v2.form_recognition import normalize_observation
from .backend_v2.observation_adapter import adapt_browser_snapshot
from .live_capture import CaptureError, capture_bound_snapshot


class RecognitionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_url: str = Field(min_length=1, max_length=2500)
    resume_id: str = Field(min_length=1, max_length=200)


def preview_projection(recognition: dict) -> dict:
    """Omit filled values, identity anchors and raw DOM/selector payloads.

    Questions/options may themselves contain personal data; this is a local
    private diagnostic, not an anonymization promise or a shareable report.
    """
    result = {key: deepcopy(recognition[key]) for key in (
        "schema_version", "issues", "coverage", "needs_observation", "read_only",
        "ready_for_review", "executable_actions")}
    keys = ("id", "question", "question_status", "control_kind", "required",
            "required_status", "options_status", "section_path", "matchable", "reasons",
            "date_precision", "multiple")
    result["fields"] = []
    for item in recognition["fields"]:
        public = {key: deepcopy(item[key]) for key in keys if key in item}
        public["options"] = [{"label": option["label"], "disabled": bool(option.get("disabled"))}
                             for option in item.get("options", [])]
        binding = item.get("entity_binding", {})
        public["entity_binding"] = {key: binding[key] for key in ("kind", "status") if key in binding}
        result["fields"].append(public)
    return result


def recognition_preview_router(*, service, require_owner: Callable[[str], None],
                               task_binding: Callable[[str], tuple[str, str]],
                               require_local: Callable[[Request], None]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/browser/{session_id}/recognition-preview")
    async def preview(session_id: str, payload: RecognitionPreviewRequest, request: Request):
        require_local(request)
        require_owner(session_id)
        original = task_binding(session_id)
        if original[0] != payload.resume_id or not original[1]:
            raise HTTPException(409, "本次连接的简历已变化，请先同步当前页；未读取或填写")

        def guard():
            require_owner(session_id)
            if task_binding(session_id) != original:
                raise CaptureError("task_changed", "读取期间本次简历或连接已变化；旧识别结果已丢弃")

        started = monotonic()
        try:
            capture = await capture_bound_snapshot(service, session_id, payload.expected_url, guard)
            # Do not call legacy review/assist: those can match facts or invoke a
            # model. D2 accepts the bounded scanner output, not client evidence.
            recognition = normalize_observation(adapt_browser_snapshot(capture.snapshot))
            guard()
            return {"schema_version": "live-recognition-preview-v1", "preview_only": True,
                    "session_id": capture.session_id, "capture_id": capture.capture_id,
                    "document_id": capture.document_id, "captured_at": capture.captured_at,
                    "url": capture.url, "title": capture.snapshot.title,
                    "recognition": preview_projection(recognition),
                    "elapsed_ms": round((monotonic() - started) * 1000),
                    "browser_actions_executed": 0, "model_calls_executed": 0,
                    "graph_executed": False, "persisted": False}
        except CaptureError as exc:
            raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from None
        except (ValueError, TypeError, KeyError):
            # Never echo raw page fields/validation input, or browser exception
            # messages that could include user data, into diagnostics or logs.
            raise HTTPException(422, {"code": "evidence_invalid", "message":
                "网页证据不完整或超过本轮读取上限；未生成填写计划，请在开发检查中核对页面"}) from None

    return router
