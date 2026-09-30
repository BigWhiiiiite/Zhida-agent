"""Offline regression: an incomplete SPA detail is not autonomous progress."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import storage
from app.application_agent import _allowed_actions, decide_application_step
from app.application_models import ApplicationAgentDecision, ApplicationWorkflowState
from app.browser_models import BrowserSnapshot, PageField, PreSubmitCheck
from app.models import CandidateProfile


SESSION_ID = "incomplete-detail-fixture"
URL = "https://example.italent.cn/job_detail?jobAdId=fixture-job"
MESSAGE = "岗位详情尚未完整加载，只读取到了站点公共控件。"
BLOCKER = "等待实际岗位内容，不能将公共搜索框当作申请表。"


def workflow() -> ApplicationWorkflowState:
    return ApplicationWorkflowState(
        session_id=SESSION_ID, url=URL, title="校园招聘", stage="unknown",
        job_id="fixture-job", job_title="", form_fields=0,
        message=MESSAGE, navigation_blocker=BLOCKER,
    )


def snapshot(*, search: bool = False) -> BrowserSnapshot:
    return BrowserSnapshot(
        session_id=SESSION_ID, url=URL, title="校园招聘",
        fields=[PageField(selector="#global-search", label="搜索职位", field_type="search")]
        if search else [],
    )


async def check_decisions() -> None:
    profile = CandidateProfile(name="虚构候选人", email="fixture@example.test")
    for has_search in (False, True):
        current = snapshot(search=has_search)
        # Even a stale 'ready' flag cannot turn a URL identifier into form or
        # final-review evidence. The blocker text itself is not the guard.
        check = PreSubmitCheck(url=URL, ready=True)
        for blocked in (workflow(), workflow().model_copy(update={"navigation_blocker": ""})):
            assert _allowed_actions(blocked, check, current, profile) == ["stop"]
            for desired in ("refresh", "start_application", "analyze_and_fill", "wait_for_login"):
                invented = ApplicationAgentDecision(
                    stage="job_detail", observed_stage="application_form", stage_conflict=True,
                    stage_conflict_evidence="虚构表单", goal="虚构岗位已确认", summary="已经可以投递",
                    next_action=desired, next_label="让 Agent 接管", rationale="虚构已登录",
                    can_execute=True, requires_user=False,
                )
                with patch("app.application_agent.configured_model", return_value=(object(), object())) as model, \
                        patch("app.application_agent.Runner.run", return_value=SimpleNamespace(final_output=invented)) as runner:
                    for _ in range(3):
                        decision = await decide_application_step(blocked, current, profile, check)
                        assert decision.stage == "unknown" and decision.next_action == "stop"
                        assert not decision.can_execute and decision.requires_user
                        assert "手动重新识别" in decision.next_label
                        assert "岗位标题" in decision.summary
                        assert MESSAGE in decision.blockers
                        assert any("全局搜索框" in item for item in decision.blockers)
                        assert any("官方岗位列表" in item for item in decision.user_questions)
                        assert "已经可以投递" not in decision.summary
                        assert not decision.stage_conflict and not decision.observed_stage
                    model.assert_not_called()
                    runner.assert_not_called()
        local = await decide_application_step(workflow(), current, profile, use_model=False)
        assert local.next_action == "stop" and not local.can_execute

    # Recovery must follow fresh authoritative evidence, not a permanent latch.
    loaded = workflow().model_copy(update={
        "stage": "job_detail", "job_title": "虚构软件开发岗", "navigation_blocker": "",
    })
    decision = await decide_application_step(loaded, snapshot(), profile, use_model=False)
    assert decision.next_action == "start_application" and decision.can_execute


def check_step_endpoint() -> None:
    from app import main as api_main

    with TemporaryDirectory() as directory, \
            patch.object(storage, "DB_PATH", Path(directory) / "api.db"), \
            patch.object(api_main, "UPLOAD_DIR", Path(directory) / "uploads"), \
            patch.dict(os.environ, {"APP_AUTH_REQUIRED": "false"}), \
            patch.object(api_main.browser_demo, "snapshot_for", return_value=snapshot(search=True)), \
            patch.object(api_main.browser_demo, "workflow_state", return_value=workflow()), \
            patch.object(api_main.browser_demo, "advance_workflow") as advance, \
            patch.object(api_main.browser_demo, "execute") as execute, \
            patch.object(api_main.browser_demo, "pre_submit_check") as pre_submit, \
            patch("app.application_agent.configured_model") as model, \
            TestClient(api_main.app) as client:
        user_id = client.get("/api/auth/me").json()["id"]
        with patch.dict(api_main.browser_session_owners, {SESSION_ID: user_id}):
            for _ in range(3):
                response = client.post(f"/api/browser/{SESSION_ID}/agent/step", json={})
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["action_taken"] == ""
                assert body["decision"]["next_action"] == "stop"
                assert body["decision"]["requires_user"] and not body["decision"]["can_execute"]
                assert body["checkpoint"]["status"] == "waiting_user"
                assert body["checkpoint"]["events"][-1]["summary"].startswith("等待用户：")
        advance.assert_not_called()
        execute.assert_not_called()
        pre_submit.assert_not_called()
        model.assert_not_called()


if __name__ == "__main__":
    asyncio.run(check_decisions())
    check_step_endpoint()
    print("application_agent_incomplete_detail_test: OK")
