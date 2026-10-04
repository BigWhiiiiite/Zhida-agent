"""No real DB, dotenv, network or browser: confirmed facts belong to Zhida."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import storage
from app.confirmed_facts import (ConfirmedFactRequest, FactRevisionConflict, FactTargets,
                                 get_fact_targets, save_confirmed_fact)
from app.models import (CandidateProfile, Education, Experience, FieldEvidence, Project,
                        ResumeProfile, ResumeRecord)
from app.task_profile import compose_task_profile


def run() -> None:
    user, other = "fixture-owner", "fixture-stranger"
    profile = ResumeProfile(
        education=[Education(school="合成研究院", degree="硕士", start_date="2025-09", major="数据科学"),
                   Education(school="合成本科大学", degree="本科", start_date="2021-09", major="计算机")],
        internships=[Experience(organization="合成实习单位", role="开发实习", start_date="2026-07")],
        projects=[Project(name="合成视频项目", start_date="2022-12", end_date="2023-12"),
                  Project(name="合成 Agent 项目", start_date="2026-02")],
    )
    evidence = [FieldEvidence(id=f"fixture-{section}", field_path=section, value=[
        item.model_dump(mode="json") for item in getattr(profile, section)], confidence=1,
        source_text="来自合成附件的原始引用", status="confirmed") for section in ("education", "internships", "projects")]

    def target(resume_id: str, contains: str):
        targets = get_fact_targets(resume_id)
        return targets, next(item for item in targets.records if contains in item.label)

    def payload(resume_id: str, contains: str, attribute: str, value: str):
        targets, record = target(resume_id, contains)
        return ConfirmedFactRequest(revision=targets.revision, record_key=record.record_key,
                                    attribute=attribute, value=value, confirmed=True)

    with TemporaryDirectory(prefix="zhida-fact-fixture-") as temp, \
         patch.object(storage, "DATA_DIR", Path(temp)), \
         patch.object(storage, "DB_PATH", Path(temp) / "fixture.sqlite3"):
        storage.initialize()
        token = storage.set_current_user(user)
        try:
            for rid in ("cv-agent", "cv-backend"):
                storage.create_resume(rid, "synthetic.pdf", "synthetic-file.pdf", rid, profile, "fixture", "中文",
                                      123, "synthetic-hash", "synthetic original text", evidence)
            original = storage.get_resume("cv-agent")
            assert original is not None
            internal = dict(storage.get_resume_internal("cv-agent"))
            original_backend = storage.get_resume("cv-backend").model_dump(mode="json")
            targets = get_fact_targets("cv-agent")
            assert len(targets.records) == 5 and not targets.warnings
            assert all("rank" not in a.key and a.key != "study_mode" for t in targets.records for a in t.attributes)
            initial_key = target("cv-agent", "合成视频项目")[1].record_key
            assert initial_key != target("cv-backend", "合成视频项目")[1].record_key

            request = payload("cv-agent", "合成视频项目", "role", "核心开发")
            saved = save_confirmed_fact("cv-agent", request)
            assert saved.profile.projects[0].role == "核心开发"
            expected = original.profile.model_dump(mode="json")
            expected["projects"][0]["role"] = "核心开发"
            assert saved.profile.model_dump(mode="json") == expected
            assert saved.profile.projects[0].start_date == "2022-12"
            assert saved.profile.projects[0].end_date == "2023-12"
            assert storage.get_resume("cv-backend").model_dump(mode="json") == original_backend
            after = dict(storage.get_resume_internal("cv-agent"))
            for key in ("stored_filename", "raw_text", "content_hash", "file_size", "filename", "parser", "status"):
                assert internal[key] == after[key], key
            for item in saved.evidence:
                assert item.status == "confirmed" and item.source_text == "来自合成附件的原始引用"
                if item.field_path == "projects":
                    assert item.value[0]["role"] == "核心开发"
            history = storage.confirmed_resume_fact_history("cv-agent")
            assert len(history) == 1 and history[0]["source"] == "user_explicit_confirmation"
            assert history[0]["previous_value"] == "" and history[0]["value"] == "核心开发"
            assert history[0]["revision"] != history[0]["new_revision"]

            # Different company, same explicitly selected CV: consume true facts
            # without leaking site-option memory or borrowing another CV.
            for company in ("a.example", "b.example"):
                composed = compose_task_profile(CandidateProfile(), saved, company)
                assert composed.projects[0].role == "核心开发"
            assert compose_task_profile(CandidateProfile(), storage.get_resume("cv-backend")).projects[0].role == ""
            try:
                save_confirmed_fact("cv-agent", request)
                raise AssertionError("Stale revision was accepted")
            except FactRevisionConflict:
                pass

            # Explicit personal duties persist on one selected project/CV and
            # enter retrieval with their own source path, not a role shortcut.
            duty_text = '负责合成 Agent 工作流与表单回读验证'
            saved = save_confirmed_fact('cv-agent', payload('cv-agent', '合成 Agent 项目', 'responsibilities', duty_text))
            assert saved.profile.projects[1].responsibilities == duty_text
            assert saved.profile.projects[0].responsibilities == ''
            assert storage.get_resume('cv-backend').profile.projects[1].responsibilities == ''
            from app.job_rag import split_profile_evidence
            for company in ('a.example', 'b.example'):
                composed = compose_task_profile(CandidateProfile(), saved, company)
                assert composed.projects[1].responsibilities == duty_text
                assert any('projects[1].responsibilities' in c.source_path and c.quote == duty_text
                           for c in split_profile_evidence(composed))

            stranger = storage.set_current_user(other)
            try:
                assert storage.confirmed_resume_fact_history("cv-agent") == []
                for operation in (lambda: get_fact_targets("cv-agent"), lambda: save_confirmed_fact("cv-agent", request)):
                    try:
                        operation()
                        raise AssertionError("Cross-user CV accessed")
                    except LookupError:
                        pass
            finally:
                storage.reset_current_user(stranger)

            # Dates preserve explicitly known precision, never manufacture days.
            for value in ("2022", "2022-12", "2022-12-13"):
                saved = save_confirmed_fact("cv-agent", payload("cv-agent", "合成视频项目", "start_date", value))
                assert saved.profile.projects[0].start_date == value
            saved = save_confirmed_fact("cv-agent", payload("cv-agent", "合成视频项目", "end_date", "至今"))
            assert saved.profile.projects[0].end_date == "至今"
            for invalid in ("2022.12", "2023-02-30", "2023-13", "2023-00", "2023-1", "大概去年"):
                try:
                    save_confirmed_fact("cv-agent", payload("cv-agent", "合成视频项目", "start_date", invalid))
                    raise AssertionError(f"Invalid date accepted: {invalid}")
                except ValueError:
                    pass

            # An individual field cannot confirm an unreviewed whole section.
            with storage._connection() as conn:
                row = storage.get_resume("cv-backend")
                pending = [item.model_copy(update={"status": "pending_review"}) for item in row.evidence]
                import json
                conn.execute("UPDATE resumes SET evidence_json=? WHERE id=? AND user_id=?", (
                    json.dumps([item.model_dump(mode="json") for item in pending]), "cv-backend", user))
            pending_saved = save_confirmed_fact("cv-backend", payload("cv-backend", "合成研究院", "college", "合成计算学院"))
            assert all(item.status == "pending_review" for item in pending_saved.evidence)
            assert compose_task_profile(CandidateProfile(), pending_saved).education == []

            # Duplicate identities are unavailable and never resolved by index.
            duplicate_profile = profile.model_copy(deep=True)
            duplicate_profile.projects.append(duplicate_profile.projects[0].model_copy(deep=True))
            storage.update_resume("cv-backend", profile=duplicate_profile)
            duplicate_targets = get_fact_targets("cv-backend")
            assert duplicate_targets.warnings and all("合成视频项目" not in item.label for item in duplicate_targets.records)
            collision = payload("cv-agent", "合成 Agent 项目", "name", "合成视频项目")
            # Name alone may differ by start date; setting the same date afterward
            # must reject the now-identical identity without a partial update.
            save_confirmed_fact("cv-agent", collision)
            different_dates = get_fact_targets("cv-agent")
            renamed = next(item for item in different_dates.records if "2026-02" in item.label)
            try:
                save_confirmed_fact("cv-agent", ConfirmedFactRequest(revision=different_dates.revision,
                    record_key=renamed.record_key, attribute="start_date", value="2022-12-13", confirmed=True))
                raise AssertionError("Duplicate identity created")
            except ValueError:
                pass
            assert storage.get_resume("cv-agent").profile.projects[1].start_date == "2026-02"

            # Two users of the same CV revision race: exactly one transaction wins.
            race = payload("cv-agent", "合成研究院", "advisor", "合成导师甲")
            barrier = Barrier(2)
            def worker(value: str):
                tok = storage.set_current_user(user)
                try:
                    barrier.wait(timeout=5)
                    save_confirmed_fact("cv-agent", race.model_copy(update={"value": value}))
                    return "saved"
                except FactRevisionConflict:
                    return "conflict"
                finally:
                    storage.reset_current_user(tok)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(worker, ["合成导师甲", "合成导师乙"]))
            assert sorted(results) == ["conflict", "saved"], results

            # Use actual route functions, no app lifespan or real authentication DB.
            with patch("dotenv.load_dotenv"):
                from app import main
            fixture = FastAPI()
            @fixture.middleware("http")
            async def isolated_owner(request, call_next):
                tok = storage.set_current_user(request.headers.get("x-fixture-user", user))
                try:
                    return await call_next(request)
                finally:
                    storage.reset_current_user(tok)
            fixture.add_api_route("/api/resumes/{resume_id}/fact-targets", main.resume_fact_targets,
                                  methods=["GET"], response_model=FactTargets)
            fixture.add_api_route("/api/resumes/{resume_id}/confirmed-fact", main.confirm_resume_fact,
                                  methods=["POST"], response_model=ResumeRecord)
            with TestClient(fixture) as client:
                url = "/api/resumes/cv-agent/confirmed-fact"
                targets_url = "/api/resumes/cv-agent/fact-targets"
                assert client.get(targets_url).status_code == 200
                assert client.get(targets_url, headers={"x-fixture-user":other}).status_code == 404
                body = payload("cv-agent", "合成研究院", "college", "合成数据学院").model_dump()
                for invalid in ({**body,"confirmed":False}, {**body,"confirmed":"true"}, {**body,"confirmed":1},
                                {**body,"record_index":0}, {**body,"section":"projects"},
                                {**body,"attribute":"ranking"}, {**body,"attribute":"education.0.college"},
                                {**body,"value":""}):
                    response = client.post(url, json=invalid)
                    assert response.status_code == 422, response.text
                assert client.post(url, json=body, headers={"x-fixture-user":other}).status_code == 404
                assert client.post(url, json=body).status_code == 200
                assert client.post(url, json=body).status_code == 409
            assert storage.confirmed_resume_fact_history("cv-backend")
            storage.delete_resume("cv-backend")
            assert storage.confirmed_resume_fact_history("cv-backend") == []
            with storage._connection() as conn:
                assert conn.execute("SELECT count(*) FROM confirmed_resume_facts WHERE resume_id='cv-backend'").fetchone()[0] == 0
        finally:
            storage.reset_current_user(token)
    print("confirmed_facts_test: OK (ownership, CAS race, identities, exact scalar update, date precision, evidence, CV isolation, company reuse, API)")


if __name__ == "__main__":
    run()
