"""Offline staged-autofill API tests: temporary DB, fake DOM, mocked model.

Run `.venv/bin/python hybrid_autofill_smoke_test.py`. No uploads, real accounts,
external model calls, recruiting-site navigation, or final submissions occur.
"""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import form_agent, main, storage
from app.application_models import ApplicationWorkflowState
from app.browser_models import (ActionResult, BrowserSnapshot, ExecutionResult, FillAction,
                                FormPlan, PageField, PreSubmitCheck)
from app.models import CandidateProfile


SESSION = "hybrid-autofill-fixture"
URL = "https://jobs.example.test/application"
PROFILE = CandidateProfile(name="测试候选人", email="candidate@example.test")


class FakeBrowser:
    def __init__(self) -> None:
        self.values = {"#name": "", "#extra": ""}
        self.executions = []
        self.snapshot_calls = 0
        self.fail_selectors: set[str] = set()

    async def workflow(self, session_id: str) -> ApplicationWorkflowState:
        return ApplicationWorkflowState(session_id=session_id, url=URL, title="Synthetic form",
            stage="application_form", form_fields=5)

    async def snapshot(self, session_id: str) -> BrowserSnapshot:
        assert session_id == SESSION
        self.snapshot_calls += 1
        return BrowserSnapshot(session_id=SESSION, url=URL, title="Synthetic form", fields=[
            PageField(selector="#name", label="姓名", required=True, current_value=self.values["#name"]),
            PageField(selector="#extra", label="通知接收地址", context="请填写本人电子邮箱",
                      required=True, current_value=self.values["#extra"]),
            PageField(selector="#third-party", label="紧急联系人姓名", required=True),
            PageField(selector="#resume", label="简历附件", field_type="file", required=True),
            PageField(selector="#password", label="密码", field_type="password"),
        ])

    async def execute(self, session_id, request, *args, **kwargs) -> ExecutionResult:
        assert session_id == SESSION
        # The staged endpoint never sends a resume path/id or navigation action.
        assert not args and not kwargs and request.resume_id == ""
        assert request.min_confidence == .85
        assert main.app.state.browser_operation_lock.locked()
        self.executions.append(request.model_copy(deep=True))
        results = []
        for action in request.actions:
            assert action.action in {"fill", "select", "check"}
            failed = action.selector in self.fail_selectors
            if not failed:
                self.values[action.selector] = str(action.value)
            results.append(ActionResult(selector=action.selector, label=action.label,
                status="failed" if failed else "filled", verified=not failed,
                actual_value=self.values.get(action.selector, ""),
                message="synthetic readback mismatch" if failed else "synthetic readback OK"))
        return ExecutionResult(url=URL, completed=sum(item.verified for item in results),
            skipped=0, failed=sum(item.status == "failed" for item in results),
            verified=sum(item.verified for item in results), results=results,
            pre_submit=PreSubmitCheck(url=URL, ready=False, submit_labels=["提交申请"]))


def call(client: TestClient, phase: str):
    return client.post(f"/api/browser/{SESSION}/autofill", json={"phase": phase})


def check_staged(client: TestClient, browser: FakeBrowser) -> None:
    with patch.object(form_agent, "configured_model", side_effect=AssertionError("rules called model")) as provider, \
            patch.object(main, "create_form_plan", side_effect=AssertionError("rules called model planner")) as planner:
        response = call(client, "rules")
        assert response.status_code == 200, response.text
        assert provider.call_count == planner.call_count == 0
    body = response.json()
    assert body["phase"] == "rules" and body["execution"]["verified"] == 1
    assert browser.values["#name"] == PROFILE.name and browser.values["#extra"] == ""
    assert body["review"]["summary"]["matched"] == 1
    assert body["review"]["plan"]["routing_summary"]["model_pending"] == 1
    assert [item.selector for item in browser.executions[0].actions] == ["#name"]
    assert browser.snapshot_calls == 2  # pre-plan and post-execution, not a second model pass

    updated_profile = PROFILE.model_copy(update={"email": "updated-candidate@example.test"})

    async def fake_model(snapshot, profile, *_):
        assert {field.selector for field in snapshot.fields} == {"#extra"}
        assert profile.email == updated_profile.email
        return FormPlan(page_summary="Synthetic model", actions=[FillAction(
            selector="#extra", label="通知接收地址", action="fill", value="must-ignore-model-value@example.test",
            value_source="untrusted model claim", profile_path="email", entity_scope="",
            question_evidence="本人电子邮箱", confidence=.98,
            reason="test-only source-grounded mapping")])

    with patch.object(main, "get_profile", return_value=updated_profile), \
            patch.object(form_agent, "configured_model", return_value=(None, None)), \
            patch.object(form_agent, "_structured_plan", side_effect=fake_model) as model:
        response = call(client, "model")
        assert response.status_code == 200, response.text
        assert model.call_count == 1
    body = response.json()
    assert body["phase"] == "model" and body["execution"]["verified"] == 1
    assert browser.values["#name"] == PROFILE.name and browser.values["#extra"] == updated_profile.email
    assert [item.selector for item in browser.executions[-1].actions] == ["#extra"]
    assert browser.executions[-1].actions[0].value_source == "主档案.email"
    assert browser.executions[-1].actions[0].profile_path == "email"
    assert browser.executions[-1].actions[0].question_evidence == "本人电子邮箱"
    assert browser.executions[-1].actions[0].resolution_source == "model"
    assert body["review"]["summary"]["matched"] == 2
    assert body["review"]["plan"]["routing_summary"]["model_resolved"] == 1
    assert body["review"]["plan"]["routing_summary"]["model_pending"] == 0
    assert browser.snapshot_calls == 4

    # Retrying rules never rewrites the already matching field.
    response = call(client, "rules")
    assert response.status_code == 200
    assert browser.executions[-1].actions == []


def check_safety_filters(client: TestClient, browser: FakeBrowser) -> None:
    before_values = dict(browser.values)
    unsafe = FormPlan(actions=[
        FillAction(selector="#name", label="姓名", action="fill", value="wrong", confidence=.2),
        FillAction(selector="#extra", label="补充", action="fill", value="wrong", confidence=1, sensitive=True),
        FillAction(selector="#third-party", label="人工", action="ask_user", confidence=1),
        FillAction(selector="#resume", label="附件", action="fill", value="/do-not-upload", confidence=1),
        FillAction(selector="#password", label="密码", action="fill", value="must-not-fill", confidence=1,
                   user_confirmed=True),
        FillAction(selector="#final-submit", label="提交申请", action="check", value=True, confidence=1),
    ])
    with patch.object(main, "create_local_form_plan", return_value=unsafe):
        result = call(client, "rules")
    assert result.status_code == 200, result.text
    assert browser.executions[-1].actions == [] and browser.values == before_values
    assert result.json()["execution"]["pre_submit"]["submit_labels"] == ["提交申请"]


def check_partial_failure(client: TestClient, browser: FakeBrowser) -> None:
    browser.values = {"#name": "", "#extra": ""}
    first = call(client, "rules")
    assert first.status_code == 200 and browser.values["#name"] == PROFILE.name
    executions_before = len(browser.executions)
    with patch.object(main, "create_form_plan", side_effect=RuntimeError("synthetic model unavailable")):
        second = call(client, "model")
    assert second.status_code == 502
    assert "不会撤销" in second.json()["detail"]
    assert browser.values["#name"] == PROFILE.name and len(browser.executions) == executions_before

    # Per-field readback failure is returned as partial success, not erased.
    browser.values = {"#name": "", "#extra": ""}
    browser.fail_selectors = {"#extra"}
    plan = FormPlan(actions=[
        FillAction(selector="#name", label="姓名", action="fill", value=PROFILE.name, confidence=1),
        FillAction(selector="#extra", label="补充", action="fill", value=PROFILE.email, confidence=1),
    ])
    with patch.object(main, "create_local_form_plan", return_value=plan):
        result = call(client, "rules")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["execution"]["verified"] == body["execution"]["failed"] == 1
    assert browser.values == {"#name": PROFILE.name, "#extra": ""}
    statuses = {item["selector"]: item["status"] for item in body["review"]["comparisons"]}
    assert statuses["#name"] == "matched" and statuses["#extra"] == "missing"
    browser.fail_selectors.clear()


def check_auth_and_request_boundary(client: TestClient, browser: FakeBrowser) -> None:
    executions_before = len(browser.executions)
    assert client.post("/api/browser/not-owned/autofill", json={"phase": "rules"}).status_code == 404
    with patch.dict(main.browser_session_owners, {SESSION: "different-user"}):
        assert call(client, "rules").status_code == 404
    assert call(client, "final_submit").status_code == 422
    assert client.post(f"/api/browser/{SESSION}/autofill",
                       json={"phase": "rules", "resume_id": "forbidden-upload"}).status_code == 422
    with patch.dict(os.environ, {"APP_AUTH_REQUIRED": "true"}):
        assert call(client, "rules").status_code == 401
    assert len(browser.executions) == executions_before


def run() -> None:
    browser = FakeBrowser()
    with TemporaryDirectory() as directory, \
            patch.object(storage, "DATA_DIR", Path(directory)), \
            patch.object(storage, "DB_PATH", Path(directory) / "hybrid.db"), \
            patch.object(main, "UPLOAD_DIR", Path(directory) / "uploads"), \
            patch.dict(os.environ, {"APP_AUTH_REQUIRED": "false", "APP_AGENT_MODEL": "fixture-model",
                                    "APP_AGENT_FALLBACK_MODEL": "", "APP_AGENT_PROMPT_JSON_MODELS": ""}), \
            patch.object(main, "get_profile", return_value=PROFILE), \
            patch.object(main.browser_demo, "snapshot_for", side_effect=browser.snapshot), \
            patch.object(main.browser_demo, "workflow_state", side_effect=browser.workflow), \
            patch.object(main.browser_demo, "execute", side_effect=browser.execute), \
            patch.object(main.browser_demo, "advance_workflow", side_effect=AssertionError("unexpected navigation")), \
            patch.object(main, "_selected_resume_path", side_effect=AssertionError("unexpected upload")), \
            TestClient(main.app) as client:
        user_id = client.get("/api/auth/me").json()["id"]
        with patch.dict(main.browser_session_owners, {SESSION: user_id}):
            check_staged(client, browser)
            check_safety_filters(client, browser)
            check_partial_failure(client, browser)
            check_auth_and_request_boundary(client, browser)
    print("hybrid_autofill_smoke_test: OK (offline API, mocked browser/model, no final submission)")


if __name__ == "__main__":
    run()
