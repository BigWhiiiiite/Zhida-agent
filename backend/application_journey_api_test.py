"""HTTP journey guards: isolated objects, no files/DB/browser/model access."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application_journey_test import turn
from app.application_models import ApplicationJourneyResult
from app.models import CandidateProfile, ResumeProfile, ResumeRecord


def run():
    sid, rid, user = "journey-fixture", "cv-fixture", "user-fixture"
    now = datetime.now(timezone.utc)
    resume = ResumeRecord(id=rid, filename="fixture.pdf", label="匿名测试", parser="fixture",
                          created_at=now, updated_at=now, profile=ResumeProfile())
    profile = CandidateProfile(name="匿名测试人")
    with patch("dotenv.load_dotenv"), patch("sqlite3.connect", side_effect=AssertionError("DB forbidden")):
        from app import main
        fixture = FastAPI()
        fixture.add_api_route("/api/browser/current", main.current_browser_session, methods=["GET"])
        fixture.add_api_route("/api/browser/{session_id}/journey", main.continue_application_journey,
                              methods=["POST"], response_model=ApplicationJourneyResult)
        with patch.object(main, "current_user_id", return_value=user), \
             patch.dict(main.browser_session_owners, {sid:user}, clear=True), \
             patch.dict(main.browser_task_resumes, {sid:rid}, clear=True), \
             patch.dict(main.browser_task_epochs, {sid:"fixture-epoch"}, clear=True), \
             patch.object(main, "get_resume", return_value=resume) as source, \
             patch.object(main, "get_profile", return_value=profile) as profile_source, \
             patch.object(main, "run_application_agent_step", AsyncMock(return_value=turn("auth_required"))) as step, \
             patch.object(main, "_selected_resume_path", side_effect=AssertionError("Implicit upload forbidden")), \
             TestClient(fixture) as client:
            endpoint = f"/api/browser/{sid}/journey"
            body = {"resume_id":rid}
            assert client.get("/api/browser/current").json()["journey_version"] == 1
            response = client.post(endpoint, json=body)
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["status"] == "waiting_login" and data["steps"] == 1
            assert data["events"][0]["stage"] == "auth_required"
            assert step.await_args.args[0] == sid and step.await_args.args[1].resume_id == rid

            for invalid in ({}, {"resume_id":""}, {**body,"max_steps":0}, {**body,"max_steps":7},
                            {**body,"allow_submit":True}, {**body,"allow_site_parse":True},
                            {**body,"actions":[]}, {**body,"selector":"#submit"},
                            {**body,"password":"synthetic"}, {**body,"code":"000000"}):
                step.reset_mock()
                assert client.post(endpoint,json=invalid).status_code == 422, invalid
                step.assert_not_awaited()

            assert client.post(endpoint,json={"resume_id":"different-cv"}).status_code == 409
            with patch.dict(main.browser_task_resumes,{},clear=True):
                assert client.post(endpoint,json=body).status_code == 409
            with patch.object(main,"current_user_id",return_value="another-user"):
                assert client.post(endpoint,json=body).status_code == 404
            source.return_value = None
            assert client.post(endpoint,json=body).status_code == 409
            source.return_value = resume
            step.assert_not_awaited()

            # Any mid-flight source/task change prevents a second safe step.
            mutations = [lambda: setattr(source,"return_value",resume.model_copy(update={"label":"new version"})),
                         lambda: setattr(profile_source,"return_value",profile.model_copy(update={"name":"new fact"})),
                         lambda: main.browser_task_epochs.update({sid:"replaced-task"}),
                         lambda: main.browser_task_resumes.update({sid:"different-cv"})]
            for mutation in mutations:
                async def changed(*args):
                    mutation()
                    return turn("application_form","start_application","analyze_and_fill",can_execute=True)
                step.reset_mock(); step.side_effect = changed
                response = client.post(endpoint,json=body)
                assert response.status_code == 409, response.text
                step.assert_awaited_once()
                source.return_value = resume; profile_source.return_value = profile
                main.browser_task_epochs[sid] = "fixture-epoch"
                main.browser_task_resumes[sid] = rid

            # Navigation URL changes are expected and must not invalidate a run.
            step.reset_mock()
            step.side_effect = [turn("application_form","start_application","analyze_and_fill",can_execute=True),
                                turn("review","analyze_and_fill",assisted="ready_for_review",ready=True)]
            response = client.post(endpoint,json=body)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "ready_for_review" and step.await_count == 2

            step.side_effect = RuntimeError("private secret provider detail")
            response = client.post(endpoint,json=body)
            assert response.status_code == 502 and "private secret" not in response.text
    print("application_journey_api_test: OK (strict payload, owner/CV/source/epoch guards, human handoff, no implicit upload)")


if __name__ == "__main__":
    run()
