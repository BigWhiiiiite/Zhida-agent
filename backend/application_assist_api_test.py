"""Assist API boundary tests; no dotenv, DB, files, browser or model calls."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application_models import (ApplicationAgentDecision, ApplicationAgentTurn,
                                    ApplicationWorkflowState)
from app.browser_models import (ApplicationAssistResult, BrowserSnapshot, PageField,
                               PreSubmitCheck)
from app.models import CandidateProfile, FieldEvidence, Project, ResumeProfile, ResumeRecord


SID, RID, USER = "assist-api-fixture", "resume-fixture", "user-fixture"
URL = "https://fixture.example.test/apply"


def run():
    now = datetime.now(timezone.utc)
    profile = CandidateProfile(name="合成测试人")
    resume = ResumeRecord(id=RID, filename="fixture.pdf", label="合成简历", parser="fixture",
        created_at=now, updated_at=now, profile=ResumeProfile(projects=[Project(name="合成项目")]),
        evidence=[FieldEvidence(id="fixture-evidence", field_path="projects", value=[], confidence=1,
                                source_text="合成资料", status="pending_review")])
    snapshot = BrowserSnapshot(session_id=SID, url=URL, title="合成表单", fields=[
        PageField(selector="#name", label="姓名")])
    workflow = ApplicationWorkflowState(session_id=SID, url=URL, title="合成表单", stage="application_form",
                                         form_fields=1, final_submit_present=True)
    check = PreSubmitCheck(url=URL)
    result = ApplicationAssistResult(status="needs_user", message="项目经历尚未核验，不会填写待核验事实",
        snapshot=snapshot, pre_submit=check)
    with patch("dotenv.load_dotenv"), patch("sqlite3.connect", side_effect=AssertionError("Database forbidden")):
        from app import main
        fixture = FastAPI()
        fixture.add_api_route("/api/browser/current", main.current_browser_session, methods=["GET"])
        fixture.add_api_route("/api/browser/{session_id}/assist", main.assist_application,
                              methods=["POST"], response_model=ApplicationAssistResult)
        fixture.add_api_route("/api/browser/{session_id}/agent/step", main.run_application_agent_step,
                              methods=["POST"], response_model=ApplicationAgentTurn)
        with patch.object(main, "current_user_id", return_value=USER), \
             patch.dict(main.browser_session_owners, {SID:USER}, clear=True), \
             patch.dict(main.browser_task_resumes, {SID:RID}, clear=True), \
             patch.object(main, "get_resume", return_value=resume), \
             patch.object(main, "_task_context", return_value=(profile, "revision")) as context, \
             patch.object(main, "_require_application_form", AsyncMock()), \
             patch.object(main, "_selected_resume_path", return_value=Path("fixture.pdf")) as path, \
             patch.object(main, "prepare_application", AsyncMock(return_value=result)) as prepare, \
             TestClient(fixture) as client:
            endpoint = f"/api/browser/{SID}/assist"
            body = {"resume_id": RID}
            assert client.get("/api/browser/current").json()["assistance_version"] == 1
            response = client.post(endpoint, json=body)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "needs_user"
            args, kwargs = prepare.await_args
            assert args[2].allow_site_parse is False and args[2].max_rounds == 3
            assert kwargs["pending_sections"] == ["项目经历"] and kwargs["resume_path"] is None
            path.assert_not_called()
            assert client.post(endpoint, json={**body,"allow_site_parse":True}).status_code == 200
            path.assert_called_once_with(RID)
            assert prepare.await_args.kwargs["resume_path"] == Path("fixture.pdf")

            for invalid in ({}, {"resume_id":""}, {**body,"max_rounds":6}, {**body,"max_rounds":0},
                            {**body,"actions":[]}, {**body,"allow_submit":True}, {**body,"selector":"#evil"}):
                prepare.reset_mock(); path.reset_mock()
                assert client.post(endpoint, json=invalid).status_code == 422, invalid
                prepare.assert_not_awaited(); path.assert_not_called()
            prepare.reset_mock()
            assert client.post(endpoint, json={"resume_id":"another-cv"}).status_code == 409
            with patch.dict(main.browser_task_resumes, {}, clear=True):
                assert client.post(endpoint, json=body).status_code == 409
            with patch.object(main, "current_user_id", return_value="another-user"):
                assert client.post(endpoint, json=body).status_code == 404
            with patch.object(main, "get_resume", return_value=None):
                assert client.post(endpoint, json=body).status_code == 404
            prepare.assert_not_awaited()

            # A source edit during an awaited operation invalidates the old plan.
            async def changed(*args, **kwargs):
                context.return_value = (profile, "changed-revision")
                kwargs["guard"]()
                raise AssertionError("A stale plan reached execution")
            prepare.side_effect = changed
            response = client.post(endpoint, json=body)
            assert response.status_code == 409 and "发生变化" in response.text, response.text
            prepare.side_effect = None; context.return_value = (profile, "revision")
            prepare.side_effect = RuntimeError("private provider details must not be exposed")
            response = client.post(endpoint, json=body)
            assert response.status_code == 502 and "private provider" not in response.text
            prepare.side_effect = None

            # The legacy agent button must share the same product engine and
            # preserve its human gate rather than report generic success.
            decision = ApplicationAgentDecision(stage="application_form", goal="填写", summary="填写",
                next_action="analyze_and_fill", next_label="填写", rationale="确定性可写", can_execute=True)
            checkpoint = dict(run_id="fixture-run",session_id=SID,status="waiting_user",stage="application_form",
                              url=URL,title="合成表单",updated_at=now,events=[])
            with patch.object(main.browser_demo,"snapshot_for",AsyncMock(return_value=snapshot)), \
                 patch.object(main.browser_demo,"workflow_state",AsyncMock(return_value=workflow)), \
                 patch.object(main.browser_demo,"pre_submit_check",AsyncMock(return_value=check)), \
                 patch.object(main.browser_demo,"execute",AsyncMock()) as execute, \
                 patch.object(main,"decide_application_step",AsyncMock(return_value=decision)), \
                 patch.object(main,"save_application_agent_checkpoint",return_value=checkpoint) as save:
                path.reset_mock(); prepare.reset_mock()
                response = client.post(f"/api/browser/{SID}/agent/step",json=body)
                assert response.status_code == 200,response.text
                data = response.json()
                assert data["assistance"]["status"] == "needs_user"
                assert data["decision"]["can_execute"] is False and data["decision"]["next_action"] == "stop"
                assert result.message in data["decision"]["blockers"]
                assert save.call_args.args[-1] == result.message
                prepare.assert_awaited_once(); path.assert_not_called(); execute.assert_not_awaited()
                with patch.dict(main.browser_task_resumes,{SID:""},clear=True):
                    response=client.post(f"/api/browser/{SID}/agent/step",json={})
                    assert response.status_code==409 and "选择本次" in response.text,response.text
    print("application_assist_api_test: OK (strict inputs, owner/CV/revision guards, opt-in upload, legacy result preservation)")


if __name__ == "__main__":
    run()
