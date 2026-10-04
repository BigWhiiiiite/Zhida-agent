"""PATCH resume contract regression; all storage is mocked, no .env is read."""
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import storage
from app.models import ResumeProfile, ResumeRecord


def run() -> None:
    with patch("dotenv.load_dotenv"), patch.object(
        storage, "_connection", side_effect=AssertionError("Real database access forbidden")
    ):
        from app import main

        now = datetime.now(timezone.utc)
        record = ResumeRecord(
            id="fixture", filename="fixture.pdf", label="Original", parser="fixture",
            profile=ResumeProfile(), created_at=now, updated_at=now,
        )
        calls = []

        def update(resume_id, **changes):
            calls.append(changes)
            if resume_id == "missing":
                return None
            if "profile" in changes:
                # Exercise the storage boundary that previously raised AttributeError.
                assert isinstance(changes["profile"], ResumeProfile)
                changes["profile"] = ResumeProfile.model_validate_json(
                    changes["profile"].model_dump_json()
                )
            return record.model_copy(update=changes)

        app = FastAPI()
        app.add_api_route("/api/resumes/{resume_id}", main.save_resume_route,
                          methods=["PATCH"], response_model=ResumeRecord)
        with patch.object(main, "update_resume", side_effect=update), \
                patch.object(main, "merge_into_profile") as merge, \
                patch.object(main, "save_profile") as save_master, \
                patch.object(main, "sync_edited_profile_value") as sync_master, \
                TestClient(app) as client:
            response = client.patch("/api/resumes/fixture", json={"profile": {
                "education": [{"school": "Fixture University", "major": "Data Science",
                               "start_date": "2025-09", "end_date": "2026-12"}]
            }})
            assert response.status_code == 200, response.text
            assert response.json()["profile"]["education"][0]["major"] == "Data Science"
            assert set(calls[-1]) == {"profile"}

            response = client.patch("/api/resumes/fixture", json={
                "profile": None, "label": "", "tags": [], "is_default": False,
            })
            assert response.status_code == 200, response.text
            assert calls[-1] == {"label": "", "tags": [], "is_default": False}
            assert client.patch("/api/resumes/fixture", json={}).status_code == 200
            assert calls[-1] == {}
            assert client.patch("/api/resumes/missing", json={"label": "New"}).status_code == 404
            merge.assert_not_called()
            save_master.assert_not_called()
            sync_master.assert_not_called()
    print("resume_update_regression_test: OK (typed profile, null/false/empty, 404, no master sync)")


if __name__ == "__main__":
    run()
