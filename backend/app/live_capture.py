"""A bounded, passive capture of an already-authorized browser connection.

The API caller must hold the existing browser-operation lock. This helper does
not connect, navigate, adopt another page, inspect login state, open controls,
take screenshots, or fill values. The legacy scanner may add its own DOM
locator metadata; ``read_only`` means no user-facing interaction/value write.

The document UUID is server-issued for this capture, not a persistent Safari
tab ID or proof of authorization for later captures. The live document handle
proves that one capture did not cross a document replacement, including a
same-URL reload. Safari ownership still comes from its confirmed connection.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

from .browser_models import BrowserSnapshot
from .external_browser import validate_external_url


class CaptureError(ValueError):
    """Fixed safe reason and user message; never include browser error text."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class BoundSnapshotCapture:
    capture_id: str
    document_id: str
    captured_at: str
    session_id: str
    url: str
    browser_engine: str
    window_id: int | None
    tab_index: int | None
    _snapshot_json: str = field(repr=False)
    schema_version: str = "bound-passive-capture-v1"
    read_only: bool = True
    browser_actions_executed: int = 0

    @property
    def snapshot(self) -> BrowserSnapshot:
        # Do not expose the authoritative envelope to mutable model aliases.
        return BrowserSnapshot.model_validate_json(self._snapshot_json)


def _url_identity(value: str) -> tuple:
    try:
        parts = urlsplit(validate_external_url(value))
    except (TypeError, AttributeError, ValueError):
        raise CaptureError("invalid_url", "只允许读取明确指定的公开招聘网址；未读取网页") from None
    return (parts.scheme.lower(), parts.hostname.lower(),
            parts.port or (443 if parts.scheme == "https" else 80),
            parts.path or "/", parts.query, parts.fragment)


def _fingerprint(snapshot: BrowserSnapshot) -> str:
    # Comparison only, never returned/logged/persisted: values may be private.
    # Include all scanner fields (precision, ownership and option evidence too),
    # not only counts: a same-URL SPA may replace a question without navigation.
    # Mirror every top-level evidence key retained by the D2 snapshot adapter.
    # In particular, non-executable context_fields can identify a school/degree
    # record and must never drift outside this guard. BrowserSnapshot does not
    # currently carry body text or input_manifest; do not manufacture either.
    data = snapshot.model_dump(include={
        "session_id", "url", "title", "browser_engine", "recognition_profile",
        "fields", "context_fields", "extraction_report",
    })
    return hashlib.sha256(json.dumps(data, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def capture_bound_snapshot(service, session_id: str, expected_url: str,
                                 guard: Callable[[], None], *,
                                 timeout_seconds: float = 60) -> BoundSnapshotCapture:
    """Compare two passive reads under the caller's lock; discard on any drift.

    ``guard`` rechecks caller/session authorization both before and after the
    read. It must be synchronous and raise on rejection. No submitted snapshot,
    selector, window ID, or document ID is accepted as trusted capture input.
    """
    expected = _url_identity(expected_url)
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 60):
        raise ValueError("capture timeout must be within 0–60 seconds")

    def authorized():
        try:
            guard()
        except Exception:
            raise CaptureError("authorization_changed", "招聘窗口授权已变化；已停止读取，请重新连接") from None

    authorized()
    page, context = service.page, service.context
    binding = (getattr(page, "window_id", None), getattr(page, "tab_index", None))

    def assert_binding():
        if (not session_id or service.session_id != session_id or page is None
                or context is None or service.page is not page or service.context is not context
                or binding != (getattr(page, "window_id", None), getattr(page, "tab_index", None))):
            raise CaptureError("binding_changed", "招聘窗口连接已变化；本次结果已丢弃，请重新连接")
        if page.is_closed():
            raise CaptureError("window_closed", "已连接的招聘窗口已关闭；未接管其他页面")

    async def current_url():
        # Safari needs a metadata read of the pinned window/tab, never front tab.
        value = await page.read_url() if callable(getattr(page, "read_url", None)) else page.url
        if _url_identity(value) != expected:
            raise CaptureError("url_changed", "当前招聘页与指定填写链接不一致；已停止，请核对登录状态和页面")
        return value

    assert_binding()
    document = None
    cleanup_allowed = True
    captured_at = datetime.now(timezone.utc).isoformat()

    async def assert_document():
        assert_binding()
        await current_url()
        try:
            identity = await document.evaluate(
                "original => ({same_document: original === document, url: location.href})")
        except Exception:
            raise CaptureError("document_changed", "读取期间招聘页面已重新加载；旧表单信息已丢弃") from None
        if not isinstance(identity, dict) or identity.get("same_document") is not True:
            raise CaptureError("document_changed", "读取期间招聘页面已重新加载；旧表单信息已丢弃")
        if _url_identity(identity.get("url")) != expected:
            raise CaptureError("url_changed", "读取期间招聘页面已跳转；旧表单信息已丢弃")
        assert_binding()

    def validate_snapshot(snapshot):
        if not isinstance(snapshot, BrowserSnapshot) or snapshot.session_id != session_id:
            raise CaptureError("snapshot_binding_mismatch", "读取结果不属于当前招聘窗口；已丢弃")
        if _url_identity(snapshot.url) != expected:
            raise CaptureError("snapshot_binding_mismatch", "读取结果不属于指定填写页；已丢弃")

    async def capture():
        nonlocal document
        assert_binding()
        await current_url()
        document = await page.evaluate_handle("() => document")
        # snapshot() itself never calls _require() or adopts another page.
        first = await service.snapshot(probe_options=False)
        await assert_document()
        validate_snapshot(first)
        baseline = _fingerprint(first)
        snapshot = await service.snapshot(probe_options=False)
        await assert_document()
        authorized()
        validate_snapshot(snapshot)
        if baseline != _fingerprint(snapshot):
            raise CaptureError("form_changed", "两次读取之间表单题目、选项或填写值发生变化；已丢弃，请待页面稳定后重试")
        return BoundSnapshotCapture(
            capture_id=str(uuid4()), document_id=str(uuid4()), captured_at=captured_at,
            session_id=session_id, url=snapshot.url, browser_engine=snapshot.browser_engine,
            window_id=binding[0], tab_index=binding[1],
            _snapshot_json=snapshot.model_dump_json(),
        )

    try:
        return await asyncio.wait_for(capture(), timeout=timeout_seconds)
    except asyncio.CancelledError:
        # Cancellation revokes the in-flight operation. Do not issue a new
        # browser cleanup command after the caller has cancelled the read.
        cleanup_allowed = False
        raise
    except asyncio.TimeoutError:
        raise CaptureError("capture_timeout", "读取招聘页面超时；未执行填写，请保持填写页打开后重试") from None
    except CaptureError as exc:
        if exc.code in {"authorization_changed", "binding_changed", "window_closed",
                        "url_changed", "invalid_url", "document_changed",
                        "snapshot_binding_mismatch"}:
            # The old document or authorization is already known to be gone.
            # Do not turn a rejected read into a browser cleanup operation.
            cleanup_allowed = False
        raise
    except Exception:
        raise CaptureError("capture_failed", "当前招聘窗口读取失败；没有填写或切换窗口，请检查页面和浏览器授权") from None
    finally:
        if document is not None and cleanup_allowed:
            try:
                assert_binding()
                authorized()
                await asyncio.wait_for(document.dispose(), timeout=2)
            except Exception:
                # Never retry navigation/probing, nor leak a disposal error.
                pass
