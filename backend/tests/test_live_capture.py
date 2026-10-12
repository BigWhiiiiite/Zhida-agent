"""Pure mocks for bounded live capture. No browser/network/model/user data."""
import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call
from uuid import UUID

import pytest

from app.browser_models import BrowserSnapshot, PageField
from app.live_capture import CaptureError, capture_bound_snapshot


URL = "https://synthetic.invalid/resumeEdit?postId=test#form"
SESSION = "synthetic-session"


def fixture():
    document = SimpleNamespace(
        evaluate=AsyncMock(return_value={"same_document": True, "url": URL}),
        dispose=AsyncMock(),
    )
    page = SimpleNamespace(
        url=URL, window_id=81, tab_index=2, is_closed=Mock(return_value=False),
        read_url=AsyncMock(return_value=URL), evaluate_handle=AsyncMock(return_value=document),
    )
    service = SimpleNamespace(
        page=page, context=object(), session_id=SESSION,
        snapshot=AsyncMock(return_value=BrowserSnapshot(session_id=SESSION, url=URL,
            title="匿名填写页", browser_engine="safari", fields=[])),
    )
    service._require = Mock(side_effect=AssertionError("must not adopt another page"))
    return service, page, document, Mock()


def run(service, guard, **kwargs):
    return asyncio.run(capture_bound_snapshot(service, SESSION, URL, guard, **kwargs))


def test_passive_capture_server_identity_and_immutable_snapshot():
    service, page, document, guard = fixture()
    result = run(service, guard)
    assert UUID(result.capture_id) != UUID(result.document_id)
    assert datetime.fromisoformat(result.captured_at).tzinfo is not None
    assert result.read_only and result.browser_actions_executed == 0
    assert (result.window_id, result.tab_index) == (81, 2)
    assert result.snapshot.url == URL
    result.snapshot.url = "https://different.invalid/"
    assert result.snapshot.url == URL
    with pytest.raises(FrozenInstanceError):
        result.document_id = "client-supplied"
    assert service.snapshot.await_args_list == [call(probe_options=False), call(probe_options=False)]
    service._require.assert_not_called()
    assert guard.call_count == 3  # Also recheck authority before browser cleanup.
    page.evaluate_handle.assert_awaited_once_with("() => document")
    document.dispose.assert_awaited_once()


@pytest.mark.parametrize("actual", ["https://synthetic.invalid/home", URL + "new",
                                    "http://127.0.0.1:5173", "about:blank"])
def test_wrong_url_fails_before_any_dom_read(actual):
    service, page, document, guard = fixture()
    page.read_url.return_value = actual
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code in {"url_changed", "invalid_url"}
    page.evaluate_handle.assert_not_awaited()
    service.snapshot.assert_not_awaited()
    document.dispose.assert_not_awaited()


def test_default_port_and_hostname_case_only_are_equivalent():
    service, page, document, guard = fixture()
    page.read_url.return_value = "https://SYNTHETIC.invalid:443/resumeEdit?postId=test#form"
    assert run(service, guard).url == URL


@pytest.mark.parametrize("change", ["session", "page", "context", "window", "tab", "closed"])
def test_binding_change_discards_result_without_fallback(change):
    service, page, document, guard = fixture()
    original = service.snapshot.return_value
    async def changed(**kwargs):
        if change == "session":
            service.session_id = "other"
        elif change == "page":
            service.page = SimpleNamespace(is_closed=lambda: False)
        elif change == "context":
            service.context = object()
        elif change == "window":
            page.window_id = 82
        elif change == "tab":
            page.tab_index = 3
        else:
            page.is_closed.return_value = True
        return original
    service.snapshot.side_effect = changed
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code in {"binding_changed", "window_closed"}
    service._require.assert_not_called()
    document.dispose.assert_not_awaited()


def test_already_closed_page_never_adopts_another_page():
    service, page, document, guard = fixture()
    page.is_closed.return_value = True
    with pytest.raises(CaptureError, match="已关闭"):
        run(service, guard)
    page.read_url.assert_not_awaited()
    service.snapshot.assert_not_awaited()
    service._require.assert_not_called()


def test_same_url_reload_is_not_a_valid_capture():
    service, page, document, guard = fixture()
    document.evaluate.return_value = {"same_document": False, "url": URL}
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code == "document_changed"
    document.dispose.assert_not_awaited()
    assert service.snapshot.await_count == 1


def test_same_url_reload_during_second_snapshot_discards_result():
    service, page, document, guard = fixture()
    document.evaluate.side_effect = [
        {"same_document": True, "url": URL}, {"same_document": False, "url": URL}]
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code == "document_changed"
    assert service.snapshot.await_count == 2
    document.dispose.assert_not_awaited()


@pytest.mark.parametrize("changes", [
    {"question_text": "另一道题目"}, {"options": ["另一选项"]},
    {"current_value": "匿名新值"}, {"container_key": "other-record"},
    {"date_precision": "month"}, {"required": True},
])
def test_same_document_dynamic_field_drift_is_rejected(changes):
    service, page, document, guard = fixture()
    first = service.snapshot.return_value.model_copy(deep=True)
    first.fields = [PageField(selector="#synthetic", question_text="匿名题目", options=["选项甲"])]
    second = first.model_copy(deep=True)
    second.fields[0] = second.fields[0].model_copy(update=changes)
    service.snapshot.side_effect = [first, second]
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code == "form_changed"
    assert "匿名" not in str(error.value)
    assert service.snapshot.await_count == 2
    document.dispose.assert_awaited_once()


def test_second_equal_snapshot_is_the_returned_observation():
    service, page, document, guard = fixture()
    first = service.snapshot.return_value.model_copy(deep=True)
    second = first.model_copy(deep=True)
    # Adapter discards this transport-only note; all retained evidence is equal.
    second.site_route.note = "第二次观察的传输说明"
    service.snapshot.side_effect = [first, second]
    assert run(service, guard).snapshot.site_route.note == "第二次观察的传输说明"


@pytest.mark.parametrize("change", ["current_value", "question_text", "container_key", "remove"])
def test_context_only_anchor_drift_is_rejected(change):
    service, page, document, guard = fixture()
    first = service.snapshot.return_value.model_copy(deep=True)
    first.context_fields = [PageField(selector="#school-context", question_text="学校名称",
        current_value="合成大学甲", semantic_key="education.school", container_key="record-a")]
    second = first.model_copy(deep=True)
    if change == "remove":
        second.context_fields = []
    else:
        setattr(second.context_fields[0], change, "合成变更")
    assert first.fields == second.fields == []
    service.snapshot.side_effect = [first, second]
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code == "form_changed"
    assert "合成" not in str(error.value)
    document.dispose.assert_awaited_once()


@pytest.mark.parametrize("key,value", [
    ("title", "另一岗位填写页"), ("recognition_profile", "another-template"),
    ("browser_engine", "chromium"),
])
def test_page_level_retained_evidence_drift_is_rejected(key, value):
    service, page, document, guard = fixture()
    first = service.snapshot.return_value.model_copy(deep=True)
    second = first.model_copy(update={key: value}, deep=True)
    service.snapshot.side_effect = [first, second]
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code == "form_changed"


def test_navigation_during_final_atomic_document_check_rejected():
    service, page, document, guard = fixture()
    document.evaluate.return_value = {"same_document": True, "url": URL.replace("test#", "other#")}
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code == "url_changed"
    document.dispose.assert_not_awaited()


@pytest.mark.parametrize("stage", ["before", "after"])
def test_guard_failure_is_safe_and_does_not_publish(stage):
    service, page, document, guard = fixture()
    guard.side_effect = ([None, RuntimeError("secret-auth-detail")] if stage == "after"
                         else RuntimeError("secret-auth-detail"))
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code == "authorization_changed"
    assert "secret" not in str(error.value)
    if stage == "before":
        page.read_url.assert_not_awaited()
    document.dispose.assert_not_awaited()


@pytest.mark.parametrize("change", ["session_id", "url"])
def test_snapshot_cannot_claim_another_binding(change):
    service, page, document, guard = fixture()
    setattr(service.snapshot.return_value, change,
            "other-session" if change == "session_id" else "https://synthetic.invalid/other")
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code == "snapshot_binding_mismatch"
    document.dispose.assert_not_awaited()


def test_timeout_cancels_only_capture_and_cleans_handle():
    service, page, document, guard = fixture()
    cancelled = []
    async def slow(**kwargs):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    service.snapshot.side_effect = slow
    with pytest.raises(CaptureError) as error:
        run(service, guard, timeout_seconds=.01)
    assert error.value.code == "capture_timeout"
    assert cancelled == [True]
    document.dispose.assert_awaited_once()
    assert service.page is page and not page.is_closed()


def test_transport_error_and_cleanup_error_never_expose_raw_text():
    service, page, document, guard = fixture()
    service.snapshot.side_effect = RuntimeError("secret-page-data")
    document.dispose.side_effect = RuntimeError("secret-cleanup-data")
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code == "capture_failed"
    assert "secret" not in str(error.value)
    document.dispose.assert_awaited_once()


def test_external_cancellation_propagates_without_browser_cleanup():
    service, page, document, guard = fixture()
    async def cancelled(**kwargs):
        raise asyncio.CancelledError()
    service.snapshot.side_effect = cancelled
    with pytest.raises(asyncio.CancelledError):
        run(service, guard)
    document.dispose.assert_not_awaited()


def test_navigation_after_snapshot_skips_all_document_cleanup():
    service, page, document, guard = fixture()
    page.read_url.side_effect = [URL, "https://other-employer.invalid/login"]
    with pytest.raises(CaptureError) as error:
        run(service, guard)
    assert error.value.code == "url_changed"
    document.evaluate.assert_not_awaited()
    document.dispose.assert_not_awaited()


def test_timeout_with_revoked_authority_does_not_cleanup():
    service, page, document, guard = fixture()
    async def slow(**kwargs):
        await asyncio.sleep(10)
    service.snapshot.side_effect = slow
    guard.side_effect = [None, RuntimeError("revoked")]
    with pytest.raises(CaptureError) as error:
        run(service, guard, timeout_seconds=.01)
    assert error.value.code == "capture_timeout"
    document.dispose.assert_not_awaited()
