"""No-progress propagation into product journey; no browser, network or DB."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application_models import (ApplicationAgentCheckpoint, ApplicationAgentDecision,
                                   ApplicationJourneyResult, ApplicationWorkflowState, WorkflowAdvanceRequest)
from app.browser_models import BrowserSnapshot, NavigationCandidate
from app.browser_service import BrowserDemoService, NavigationNoProgressError
from app.models import CandidateProfile, ResumeProfile, ResumeRecord


SID, RID, USER = "pause-fixture", "cv-fixture", "user-fixture"
URL = "https://pause-fixture.example.test/campus"
HOME = ApplicationWorkflowState(session_id=SID, url=URL, title="合成招聘首页", stage="homepage",
    navigation_candidates=[NavigationCandidate(id="fixture-entrance", label="校园招聘", url=URL, kind="browse_jobs")])


async def service_tests():
    service = BrowserDemoService()
    service.session_id = SID
    service.page = SimpleNamespace(is_closed=lambda:False, url=URL)
    async def inspect(*args):
        return HOME.model_copy(deep=True)
    with patch("app.browser_service.inspect_application_page", AsyncMock(side_effect=inspect)), \
         patch.object(service, "_validate_url", side_effect=lambda url:url), \
         patch("app.browser_service.execute_navigation", AsyncMock()) as click:
        try:
            await service.advance_workflow(SID, WorkflowAdvanceRequest(intent="browse_jobs"))
            raise AssertionError("No-progress reported as success")
        except NavigationNoProgressError as exc:
            assert exc.workflow.stage == "homepage" and exc.workflow.navigation_blocker
            assert "未观察到" in str(exc)
        click.assert_awaited_once()
        state = await service.workflow_state(SID)
        assert state.navigation_blocker and state.stage == "homepage"
        try:
            await service.advance_workflow(SID, WorkflowAdvanceRequest(intent="browse_jobs"))
            raise AssertionError("No-progress navigation retried")
        except ValueError as exc:
            assert "阻止重复" in str(exc)
        click.assert_awaited_once()


def api_tests():
    now = datetime.now(timezone.utc)
    snapshot = BrowserSnapshot(session_id=SID, url=URL, title=HOME.title, fields=[])
    blocked = HOME.model_copy(update={"navigation_blocker":"入口点击后页面未前进，停止重复；请选择真正的岗位入口"})
    initial = ApplicationAgentDecision(stage="homepage", goal="进入岗位列表", summary="浏览真实入口",
        next_action="browse_jobs", next_label="进入校园招聘", rationale="fixture", can_execute=True,
        requires_user=False, candidate_id="fixture-entrance")
    paused = initial.model_copy(update={"next_action":"stop", "summary":blocked.navigation_blocker,
        "next_label":"查看暂停原因", "can_execute":False, "requires_user":True})
    resume = ResumeRecord(id=RID, filename="anonymous.pdf", label="匿名简历", parser="fixture",
        profile=ResumeProfile(), created_at=now, updated_at=now)
    checkpoint = ApplicationAgentCheckpoint(run_id="pause-run", session_id=SID, status="waiting_user",
        stage="homepage", url=URL, title=HOME.title, updated_at=now)
    def checkpoint_saved(*args):
        return {**checkpoint.model_dump(mode="json"), "events":[{
            "action":args[-2], "stage":args[2], "summary":args[-1], "created_at":now.isoformat()}]}
    with patch("dotenv.load_dotenv"), patch("sqlite3.connect", side_effect=AssertionError("DB forbidden")):
        from app import main
        fixture = FastAPI()
        fixture.add_api_route("/api/browser/{session_id}/journey", main.continue_application_journey,
            methods=["POST"], response_model=ApplicationJourneyResult)
        with patch.object(main,"current_user_id",return_value=USER), \
             patch.dict(main.browser_session_owners,{SID:USER},clear=True), \
             patch.dict(main.browser_task_resumes,{SID:RID},clear=True), \
             patch.dict(main.browser_task_epochs,{SID:"epoch-fixture"},clear=True), \
             patch.object(main,"get_resume",return_value=resume), \
             patch.object(main,"get_profile",return_value=CandidateProfile()), \
             patch.object(main,"_task_profile",return_value=CandidateProfile()), \
             patch.object(main.browser_demo,"snapshot_for",AsyncMock(return_value=snapshot)), \
             patch.object(main.browser_demo,"workflow_state",AsyncMock(side_effect=[HOME,blocked])), \
             patch.object(main.browser_demo,"advance_workflow",AsyncMock(side_effect=NavigationNoProgressError("已尝试操作，但未观察到进展；停止重复",blocked))) as click, \
             patch.object(main.browser_demo,"pre_submit_check",AsyncMock(side_effect=AssertionError("Homepage has no form"))), \
             patch.object(main,"decide_application_step",AsyncMock(side_effect=[initial,paused])) as model, \
             patch.object(main,"_run_application_assist",AsyncMock(side_effect=AssertionError("No autofill"))), \
             patch.object(main,"save_application_agent_checkpoint",side_effect=checkpoint_saved) as save, \
             TestClient(fixture) as client:
            result = client.post(f"/api/browser/{SID}/journey",json={"resume_id":RID,"max_steps":4})
            assert result.status_code == 200, result.text
            data = result.json()
            assert data["status"] == "blocked" and data["steps"] == 1
            assert data["turn"]["workflow"]["stage"] == "homepage"
            assert data["turn"]["snapshot"]["session_id"] == SID
            assert data["turn"]["execution"] is None and data["turn"]["assistance"] is None
            assert "未观察到" in data["events"][0]["message"]
            assert "已执行" not in save.call_args.args[-1]
            click.assert_awaited_once()
            assert model.await_args_list[1].kwargs == {"use_model":False}


if __name__ == "__main__":
    asyncio.run(service_tests())
    api_tests()
    print("navigation_pause_test: OK (one attempt, typed pause, fresh stage retained, HTTP blocked result, no writes/retry)")
