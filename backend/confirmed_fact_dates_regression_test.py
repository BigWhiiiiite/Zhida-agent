"""Offline API regression: confirmed dates must not create a definite reversal."""
from __future__ import annotations

from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import storage
from app.confirmed_facts import (ConfirmedFactRequest, get_fact_targets,
                                 prepare_confirmed_fact)
from app.models import (CandidateProfile, Education, Experience, FieldEvidence,
                        Project, ResumeProfile, ResumeRecord)
from app.task_profile import compose_task_profile


def run() -> None:
    owner = "synthetic-date-owner"
    sequence = count()
    constructors = {
        "education": lambda **dates: Education(school="合成学院", degree="本科", **dates),
        "internships": lambda **dates: Experience(organization="合成单位", role="实习", **dates),
        "projects": lambda **dates: Project(name="合成项目", **dates),
    }

    def create(section: str, start: str, end: str) -> str:
        resume_id = f"synthetic-cv-{next(sequence)}"
        record = constructors[section](start_date=start, end_date=end)
        profile = ResumeProfile(**{section: [record]})
        evidence = [FieldEvidence(id=f"synthetic-{section}", field_path=section,
            value=[record.model_dump(mode="json")], confidence=1,
            source_text="合成附件原文", status="confirmed")]
        storage.create_resume(resume_id, "synthetic.pdf", "synthetic-original.pdf", resume_id,
            profile, "fixture", "中文", 10, "synthetic-hash", "合成附件原文", evidence)
        return resume_id

    def database_snapshot() -> dict:
        # Include other CVs, original attachment metadata/text, master and audit.
        with storage._connection() as conn:
            return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
                    for table in ("resumes", "candidate_profiles", "confirmed_resume_facts")}

    with TemporaryDirectory(prefix="zhida-fact-date-regression-") as temporary, \
            patch.object(storage, "DATA_DIR", Path(temporary)), \
            patch.object(storage, "DB_PATH", Path(temporary) / "synthetic.sqlite3"):
        storage.initialize()
        token = storage.set_current_user(owner)
        try:
            # Only mount real route functions; no production lifespan/auth/database.
            with patch("dotenv.load_dotenv"):
                from app import main
            fixture = FastAPI()

            @fixture.middleware("http")
            async def fixture_owner(request, call_next):
                request_token = storage.set_current_user(owner)
                try:
                    return await call_next(request)
                finally:
                    storage.reset_current_user(request_token)

            fixture.add_api_route("/api/resumes/{resume_id}/confirmed-fact", main.confirm_resume_fact,
                                  methods=["POST"], response_model=ResumeRecord)
            master_before = storage.save_profile(ResumeProfile(name="合成候选人")).model_dump(mode="json")
            other_id = create("projects", "2023", "2024")
            other_before = dict(storage.get_resume_internal(other_id))
            rejected, accepted = 0, 0
            with TestClient(fixture) as client:
                def check(section: str, start: str, end: str, attribute: str, value: str,
                          expected_status: int) -> None:
                    nonlocal rejected, accepted
                    resume_id = create(section, start, end)
                    targets = get_fact_targets(resume_id)
                    payload = ConfirmedFactRequest(revision=targets.revision,
                        record_key=targets.records[0].record_key, attribute=attribute,
                        value=value, confirmed=True)
                    original = storage.get_resume(resume_id)
                    original_model = original.model_dump(mode="json")
                    original_row = dict(storage.get_resume_internal(resume_id))
                    before = database_snapshot()
                    response = client.post(f"/api/resumes/{resume_id}/confirmed-fact", json=payload.model_dump())
                    stored = storage.get_resume(resume_id)
                    record = getattr(stored.profile, section)[0]
                    history = storage.confirmed_resume_fact_history(resume_id)
                    context = (section, start, end, attribute, value)
                    if response.status_code != expected_status:
                        company_profile = compose_task_profile(CandidateProfile(), stored, "other-company.example")
                        company_record = getattr(company_profile, section)[0]
                        raise AssertionError(
                            f"{context}: expected HTTP {expected_status}, got {response.status_code}; "
                            f"stored dates={record.start_date}/{record.end_date}; "
                            f"audit rows={len(history)}; other-company dates="
                            f"{company_record.start_date}/{company_record.end_date}")
                    if expected_status == 422:
                        assert "结束时间不能早于开始时间" in response.json()["detail"], response.text
                        assert database_snapshot() == before, f"Rejected fact partially wrote data: {context}"
                        try:
                            prepare_confirmed_fact(original, payload)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError(f"Direct preparation accepted reversal: {context}")
                        assert original.model_dump(mode="json") == original_model
                        rejected += 1
                    else:
                        expected_profile = original.profile.model_dump(mode="json")
                        expected_profile[section][0][attribute] = value
                        assert stored.profile.model_dump(mode="json") == expected_profile, context
                        assert record.start_date == (value if attribute == "start_date" else start), context
                        assert record.end_date == (value if attribute == "end_date" else end), context
                        assert len(history) == 1 and history[0]["value"] == value, context
                        assert stored.evidence[0].value == expected_profile[section], context
                        assert stored.evidence[0].source_text == "合成附件原文"
                        assert stored.evidence[0].status == "confirmed"
                        after_row = dict(storage.get_resume_internal(resume_id))
                        for key in original_row.keys() - {"profile_json", "evidence_json", "updated_at"}:
                            assert after_row[key] == original_row[key], (context, key)
                        for company in ("first-company.example", "second-company.example"):
                            composed = compose_task_profile(CandidateProfile(), stored, company)
                            assert getattr(composed, section)[0].model_dump(mode="json") == expected_profile[section][0]
                        accepted += 1
                    assert dict(storage.get_resume_internal(other_id)) == other_before
                    assert storage.get_profile().model_dump(mode="json") == master_before

                # Every pair is certainly reversed even at its recorded precision.
                reversals = (
                    ("2026-07", "2025-12"),
                    ("2026", "2025"),
                    ("2026-02", "2026-01"),
                    ("2026-03-01", "2026-02"),
                    ("2024-03-01", "2024-02-29"),
                    ("2026-07-02", "2026-07-01"),
                )
                # Overlapping precision, equality, unknowns and '至今' are not contradictions.
                allowed = (
                    ("2026", "2026-01-01"),
                    ("2026-12-31", "2026"),
                    ("2026-07", "2026-07-01"),
                    ("2026-07-31", "2026-07"),
                    ("2024-02-29", "2024-02"),
                    ("2026-07-01", "2026-07-01"),
                    ("2026", "2026"),
                    ("2026-07", "2026-07"),
                    ("0001", "9999"),
                    ("2026-07", "至今"),
                )
                for section in constructors:
                    for start, end in reversals:
                        check(section, start, "9999", "end_date", end, 422)
                        check(section, "0001", end, "start_date", start, 422)
                    for start, end in allowed:
                        check(section, start, "", "end_date", end, 200)
                        check(section, "", end, "start_date", start, 200)
                    for unknown in ("", "未提供", "2026年", "2026-02-30"):
                        check(section, unknown, "", "end_date", "2025-01", 200)
                        check(section, "", unknown, "start_date", "2027-01", 200)
                    # Repair either endpoint of a legacy reversal independently.
                    check(section, "2026-07", "2025-12", "end_date", "2026-08", 200)
                    check(section, "2026-07", "2025-12", "start_date", "2025-11", 200)
            print(f"confirmed_fact_dates_regression_test: OK ({rejected} reversed API writes rejected atomically; "
                  f"{accepted} precision/unknown/current/repair writes; originals, master, CV isolation and company reuse)")
        finally:
            storage.reset_current_user(token)


if __name__ == "__main__":
    run()
