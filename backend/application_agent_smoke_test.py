"""Offline safety checks for the application orchestration agent."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import storage
from app.application_agent import decide_application_step
from app.application_models import ApplicationAgentCheckpoint, ApplicationAgentDecision, ApplicationWorkflowState
from app.browser_models import ApplicationTarget, NavigationCandidate, BrowserSnapshot, PageField, PreSubmitCheck
from app.ats_registry import resolve_site_route
from app.models import CandidateProfile


def state(stage: str, **updates) -> ApplicationWorkflowState:
    return ApplicationWorkflowState(
        session_id="agent-session", url="https://jobs.example.test/apply",
        title="Campus application", stage=stage, **updates,
    )


def check_api_transition() -> None:
    from app import main as api_main

    stage = ["job_detail"]

    async def workflow(_):
        return state(stage[0])

    async def snapshot(_):
        return BrowserSnapshot(session_id="agent-session", url="https://jobs.example.test/apply",
                               title="Fixture", fields=[])

    async def advance(_, request):
        assert request.intent == "start_application"
        stage[0] = "verification_required"
        return state(stage[0])

    with TemporaryDirectory() as directory, \
            patch.object(storage, "DB_PATH", Path(directory) / "api.db"), \
            patch.object(api_main, "UPLOAD_DIR", Path(directory) / "uploads"), \
            patch.dict(os.environ, {"APP_AUTH_REQUIRED": "false"}), \
            patch.object(api_main.browser_demo, "snapshot_for", side_effect=snapshot), \
            patch.object(api_main.browser_demo, "workflow_state", side_effect=workflow), \
            patch.object(api_main.browser_demo, "advance_workflow", side_effect=advance), \
            patch("app.application_agent.configured_model", side_effect=RuntimeError("offline")), \
            TestClient(api_main.app) as client:
        user_id = client.get("/api/auth/me").json()["id"]
        with patch.dict(api_main.browser_session_owners, {"agent-session": user_id}):
            assert client.post("/api/browser/not-owned/agent/assess").status_code == 404
            response = client.post("/api/browser/agent-session/agent/step", json={})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["action_taken"] == "start_application"
            assert body["workflow"]["stage"] == "verification_required"
            assert body["decision"]["next_action"] == "wait_for_verification"
            assert not body["decision"]["can_execute"]
            assert body["checkpoint"]["status"] == "waiting_user"
            blocked = client.post("/api/browser/agent-session/agent/step", json={}).json()
            assert blocked["action_taken"] == ""


async def main() -> None:
    snapshot = BrowserSnapshot(
        session_id="agent-session", url="https://jobs.example.test/apply",
        title="Campus application", fields=[],
    )
    profile = CandidateProfile(name="测试候选人", email="candidate@example.test")

    # Proxy failure must degrade to deterministic decisions for every stage.
    with patch("app.application_agent.configured_model", side_effect=RuntimeError("offline")):
        expectations = {
            "job_detail": "start_application",
            "registration_required": "wait_for_registration",
            "auth_required": "wait_for_login",
            "verification_required": "wait_for_verification",
            "profile_form": "refresh",
            "application_form": "refresh",
            "review": "refresh",
            "unknown": "refresh",
        }
        for stage_name, expected in expectations.items():
            decision = await decide_application_step(state(stage_name), snapshot, profile)
            assert decision.next_action == expected
        verified = await decide_application_step(
            state("application_form", safe_next_present=True, safe_next_label="下一页", form_fields=1),
            snapshot.model_copy(update={"fields": [PageField(selector="#name",label="姓名",current_value=profile.name)]}),
            profile, PreSubmitCheck(url=snapshot.url, ready=True),
        )
        assert verified.next_action == "continue_application" and verified.can_execute
        empty = await decide_application_step(
            state("application_form", safe_next_present=True, safe_next_label="下一页"),
            snapshot, profile, PreSubmitCheck(url=snapshot.url, ready=True),
        )
        assert empty.next_action == "refresh"

    # Even if the model asks to auto-fill during verification, code limits it to
    # the actions permitted by the authoritative stage.
    malicious = ApplicationAgentDecision(
        stage="application_form", goal="绕过验证", summary="直接填写",
        next_action="analyze_and_fill", next_label="自动填写", rationale="测试",
        can_execute=True,
    )
    with patch("app.application_agent.configured_model", return_value=(object(), object())), \
            patch("app.application_agent.Agent", return_value=object()), \
            patch("app.application_agent.Runner.run",
                  return_value=SimpleNamespace(final_output=malicious)):
        guarded = await decide_application_step(
            state("verification_required"), snapshot, profile,
        )
    assert guarded.stage == "verification_required"
    assert guarded.next_action == "wait_for_verification"
    assert guarded.requires_user and not guarded.can_execute
    assert guarded.next_label != "自动填写"

    disagreement = ApplicationAgentDecision(
        stage="job_detail", observed_stage="job_list", stage_conflict=True,
        stage_conflict_evidence="只有职位筛选控件，未发现具体岗位", goal="核对阶段",
        summary="需要重读", next_action="start_application", next_label="进入申请",
        rationale="导航页不能填写",
    )
    with patch("app.application_agent.configured_model", return_value=(object(), object())), \
            patch("app.application_agent.Agent", return_value=object()), \
            patch("app.application_agent.Runner.run", return_value=SimpleNamespace(final_output=disagreement)):
        corrected = await decide_application_step(state("job_detail"), snapshot, profile)
    assert corrected.next_action == "refresh" and corrected.stage_conflict
    assert corrected.stage == "job_detail" and corrected.candidate_id == ""
    assert corrected.observed_stage == "job_list"
    selected = NavigationCandidate(id="observed-target",label="AI Agent 工程师",url="https://jobs.example.test/job/1",
                                   kind="open_job",matches_target=True)
    nav_state = state("job_list",target=ApplicationTarget(job_title="AI Agent 工程师"),
                      navigation_candidates=[selected])
    malicious_candidate = disagreement.model_copy(update={
        "stage":"job_list","observed_stage":"","stage_conflict":False,
        "next_action":"open_job","candidate_id":"invented-selector","next_label":"打开伪造入口"})
    with patch("app.application_agent.configured_model", return_value=(object(), object())), \
            patch("app.application_agent.Agent", return_value=object()), \
            patch("app.application_agent.Runner.run", return_value=SimpleNamespace(final_output=malicious_candidate)):
        nav_decision = await decide_application_step(nav_state,snapshot,profile)
    assert nav_decision.candidate_id == selected.id and nav_decision.next_label != "打开伪造入口"

    # An optional known field is still worth filling on a single-page form even
    # when all required fields are complete. The final submit itself stays gated.
    optional = snapshot.model_copy(update={"fields": [PageField(
        selector="#name", label="姓名", name="candidate_name",
    )], "site_route": resolve_site_route("https://example.italent.cn/apply")})
    decision = await decide_application_step(
        state("review", final_submit_present=True, form_fields=1), optional, profile,
        PreSubmitCheck(url=snapshot.url, ready=True), use_model=False,
    )
    assert decision.next_action == "analyze_and_fill"
    optional.fields[0].current_value = profile.name
    decision = await decide_application_step(
        state("review", final_submit_present=True, form_fields=1), optional, profile,
        PreSubmitCheck(url=snapshot.url, ready=True), use_model=False,
    )
    assert decision.next_action == "review_before_submit" and not decision.can_execute

    with TemporaryDirectory() as temporary:
        storage.DB_PATH = Path(temporary) / "agent.db"
        storage.initialize()
        token = storage.set_current_user("agent-user-a")
        try:
            first = storage.save_application_agent_checkpoint(
                "agent-session", "active", "job_detail", snapshot.url,
                snapshot.title, "assess", "准备进入申请",
            )
            second = storage.save_application_agent_checkpoint(
                "agent-session", "waiting_user", "auth_required", snapshot.url,
                snapshot.title, "start_application", "等待用户登录",
            )
            parsed = ApplicationAgentCheckpoint.model_validate(second)
            assert parsed.run_id == first["run_id"] and len(parsed.events) == 2
            other = storage.set_current_user("agent-user-b")
            try:
                assert storage.get_application_agent_checkpoint("agent-session") is None
            finally:
                storage.reset_current_user(other)
        finally:
            storage.reset_current_user(token)
    print("application_agent_smoke_test: OK")


if __name__ == "__main__":
    asyncio.run(main())
    check_api_transition()
    print("application_agent_api_transition: OK")
