"""A stable wrong-job page cannot pass the product's write boundaries."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import browser_service
from app.application_assist import prepare_application
from app.application_models import ApplicationWorkflowState, WorkflowAdvanceRequest
from app.browser_models import ApplicationAssistRequest, ApplicationTarget, BrowserSnapshot, ExecutePlanRequest
from app.models import CandidateProfile


async def rejected(call):
    try:
        await call
    except (ValueError, HTTPException) as error:
        assert "岗位编号" in str(error), str(error)
        return
    raise AssertionError("Wrong-job write boundary was not enforced")


async def run():
    sid = "wrong-job-fixture"
    url = "https://fixture.zhiye.com/form?jobAdId=wrong-job"
    target = ApplicationTarget(source_url="https://fixture.zhiye.com/detail?jobAdId=selected-job")
    page = SimpleNamespace(url=url, is_closed=lambda:False)
    service = browser_service.BrowserDemoService()
    service.page = page; service.session_id = sid; service.target = target
    state = ApplicationWorkflowState(session_id=sid,url=url,title="合成申请表",stage="application_form",
                                     form_fields=1,target=target)
    with patch.object(browser_service,"inspect_application_page",AsyncMock(return_value=state)), \
         patch.object(browser_service,"start_application",AsyncMock()) as start, \
         patch.object(browser_service,"continue_application",AsyncMock()) as next_page, \
         patch.object(service,"snapshot",AsyncMock(side_effect=AssertionError("Must stop before any form read/write"))):
        assert "岗位编号" in (await service.workflow_state(sid)).navigation_blocker
        await rejected(service.execute(sid,ExecutePlanRequest(actions=[])))
        await rejected(service.import_resume_with_site_parser(sid,Path("nonexistent-fixture.pdf"),True))
        await rejected(service.advance_workflow(sid,WorkflowAdvanceRequest(intent="continue_application")))
        state.stage = "job_detail"
        await rejected(service.advance_workflow(sid,WorkflowAdvanceRequest(intent="start_application")))
        start.assert_not_awaited(); next_page.assert_not_awaited()
        # Read-only state inspection remains possible to explain the blocker.
        assert (await service.advance_workflow(sid,WorkflowAdvanceRequest(intent="refresh"))).navigation_blocker

        state.stage = "application_form"
        with patch("dotenv.load_dotenv"), patch("sqlite3.connect",side_effect=AssertionError("DB forbidden")):
            from app import main
            with patch.object(main,"browser_demo",service):
                await rejected(main._require_application_form(sid))
        snapshot = BrowserSnapshot(session_id=sid,url=url,title="合成申请表",fields=[])
        with patch.object(service,"settled_snapshot",AsyncMock(return_value=snapshot)), \
             patch.object(service,"execute",AsyncMock()) as write:
            result = await prepare_application(service,sid,ApplicationAssistRequest(resume_id="fixture"),
                CandidateProfile(),guard=lambda:None,model_plan=AsyncMock(),stamp_plan=lambda plan:plan)
            assert result.status == "blocked" and "岗位编号" in result.message
            write.assert_not_awaited()
    print("target_identity_boundary_test: OK (stable wrong job blocked at navigation, upload, execution, API and assist; read-only refresh allowed)")


if __name__ == "__main__":
    asyncio.run(run())
