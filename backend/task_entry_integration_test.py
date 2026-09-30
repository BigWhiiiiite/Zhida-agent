"""Offline task handoff/API safety: isolated DB, no browser or model requests."""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import main, storage
from app.application_models import ApplicationWorkflowState
from app.browser_models import BrowserSnapshot
from app.job_navigation import recruitment_cycle_matches


URL = "https://careers.example.com/campus"
SESSION = "task-handoff-fixture"


def run() -> None:
    assert recruitment_cycle_matches("2027校招", "示例公司2027校园招聘")
    assert recruitment_cycle_matches("2027 Campus Recruitment", "2027届校园招聘")
    assert not recruitment_cycle_matches("2027校招", "2026校园招聘")
    assert not recruitment_cycle_matches("2027秋招", "2027春季校招")
    snapshot = BrowserSnapshot(session_id=SESSION, url=URL, title="Fixture Careers", fields=[])
    with TemporaryDirectory() as directory, \
            patch.object(storage, "DATA_DIR", Path(directory)), \
            patch.object(storage, "DB_PATH", Path(directory) / "tasks.db"), \
            patch.object(main, "UPLOAD_DIR", Path(directory) / "uploads"), \
            patch.dict(os.environ, {"APP_AUTH_REQUIRED": "false"}), \
            patch.object(main.browser_demo, "session_id", None), \
            patch.object(main.browser_demo, "start", new_callable=AsyncMock, return_value=snapshot) as start, \
            patch.object(main.browser_demo, "execute", new_callable=AsyncMock) as execute, \
            patch.object(main.browser_demo, "import_resume_with_site_parser", new_callable=AsyncMock) as upload, \
            patch.object(main.browser_demo, "close", new_callable=AsyncMock), \
            TestClient(main.app) as client:
        user_id = client.get("/api/auth/me").json()["id"]
        with patch.dict(main.browser_session_owners, {}, clear=True):
            response = client.post("/api/browser/start", json={"url": URL, "target": {
                "company": "Fixture Corp", "job_title": "AI Agent开发工程师", "city": "北京",
                "recruitment_cycle": "2027", "source_url": URL,
            }})
            assert response.status_code == 200, response.text
            assert start.await_args.kwargs["target"].job_title == "AI Agent开发工程师"
            assert start.await_args.args == (URL, user_id)
            assert main.browser_session_owners[SESSION] == user_id

            # Creating a new task cannot close another in-progress session.
            with patch.object(main.browser_demo, "session_id", SESSION):
                restored = client.get("/api/browser/current").json()
                assert restored == {"session_id": SESSION, "occupied": True}
                with patch.dict(main.browser_session_owners, {SESSION: "some-other-user"}):
                    hidden = client.get("/api/browser/current").json()
                    assert hidden == {"session_id": None, "occupied": True}
                blocked = client.post("/api/browser/start", json={"url": URL})
                assert blocked.status_code == 409, blocked.text
                assert start.await_count == 1

            for stage in ("homepage", "job_list", "job_detail", "auth_required", "unknown"):
                workflow = ApplicationWorkflowState(session_id=SESSION, url=URL,
                    title="Fixture", stage=stage, form_fields=5)
                with patch.object(main.browser_demo, "workflow_state", new_callable=AsyncMock,
                                  return_value=workflow):
                    for path, body in (
                        ("autofill", {"phase": "rules"}),
                        ("execute", {"actions": []}),
                        ("native-resume", {"resume_id": "never-read"}),
                    ):
                        blocked = client.post(f"/api/browser/{SESSION}/{path}", json=body)
                        assert blocked.status_code == 409, (stage, path, blocked.text)
            empty = ApplicationWorkflowState(session_id=SESSION, url=URL,
                title="Empty", stage="review", form_fields=0)
            with patch.object(main.browser_demo, "workflow_state", new_callable=AsyncMock,
                              return_value=empty):
                assert client.post(f"/api/browser/{SESSION}/autofill",
                                   json={"phase": "rules"}).status_code == 409
            assert execute.await_count == upload.await_count == 0
            with patch.dict(main.browser_session_owners, {SESSION: "some-other-user"}):
                assert client.post(f"/api/browser/{SESSION}/execute", json={"actions": []}).status_code == 404
    print("task_entry_integration_test: OK (task forwarded; busy, non-form, zero-field and ownership guards)")


if __name__ == "__main__":
    run()
