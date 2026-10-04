"""Product integration: real journey/step/assist/grounding/facts, isolated I/O.

Synthetic browser observations and mock model replies are the only substitutes.
No production DB, dotenv, credentials, real browser, uploads or network calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import application_agent, form_agent, storage
from app.application_models import (ApplicationAgentDecision, ApplicationJourneyResult,
                                    ApplicationWorkflowState)
from app.browser_models import (ActionResult, BrowserSnapshot, ExecutionResult, FillAction,
                                FormPlan, PageField, PreSubmitCheck, RequiredFieldIssue)
from app.confirmed_facts import FactTargets
from app.models import CandidateProfile, FieldEvidence, Project, ResumeProfile, ResumeRecord


SID, RID, USER = "journey-product-fixture", "cv-agent-fixture", "user-product-fixture"
TITLE = "合成企业 Agent 开发岗（2027 校招）"


class SyntheticBrowser:
    def __init__(self):
        self.stage = "job_detail"
        self.host = "company-a.example.test"
        self.include_model_field = True
        self.include_declaration = True
        self.optional_unknown = False
        self.safe_next = False
        self.page = SimpleNamespace(url="")
        self.reset_form()
        self.navigation = []
        self.executions = []
        self.reads = 0
        self.sync_url()

    def reset_form(self):
        self.values = {"#name": "", "#extra": "", "#project-name": "合成 Agent 项目", "#project-role": ""}

    def sync_url(self):
        self.page.url = f"https://{self.host}/{self.stage}"

    async def snapshot_for(self, sid):
        assert sid == SID
        self.reads += 1
        self.sync_url()
        if self.stage != "application_form":
            fields = [PageField(selector="#password", label="密码", field_type="password")] if self.stage == "auth_required" else []
        else:
            fields = [
                PageField(selector="#name", label="姓名", required=True, current_value=self.values["#name"]),
                PageField(selector="#extra", label="通知接收地址", context="请填写本人电子邮箱", required=True,
                          current_value=self.values["#extra"]),
                PageField(selector="#project-name", label="项目名称", semantic_key="project.name", section="项目经历",
                          container_key="project-one", required=True, current_value=self.values["#project-name"]),
                PageField(selector="#project-role", label="项目角色", semantic_key="project.role", section="项目经历",
                          container_key="project-one", required=True, current_value=self.values["#project-role"]),
                PageField(selector="#declaration", label="我承诺申请材料真实有效", field_type="checkbox", required=True),
            ]
            if not self.include_model_field:
                fields = [field for field in fields if field.selector != "#extra"]
            if not self.include_declaration:
                fields = [field for field in fields if field.selector != "#declaration"]
            if self.optional_unknown:
                fields.append(PageField(selector="#optional-unknown", label="紧急联系人姓名", required=False))
        return BrowserSnapshot(session_id=sid, url=self.page.url, title=TITLE, fields=fields)

    async def settled_snapshot(self, sid):
        return await self.snapshot_for(sid)

    async def workflow_state(self, sid):
        self.sync_url()
        return ApplicationWorkflowState(session_id=sid, url=self.page.url, title=TITLE, stage=self.stage,
            job_id="fixture-job-1", job_title=TITLE, authenticated=self.stage == "application_form",
            form_fields=5 if self.stage == "application_form" else 0,
            final_submit_present=self.stage == "application_form" and not self.safe_next,
            safe_next_present=self.safe_next, safe_next_label="下一步" if self.safe_next else "")

    async def advance_workflow(self, sid, request):
        if request.intent == "continue_application":
            assert self.stage == "application_form" and self.safe_next
            assert not self.optional_unknown, "Optional user question was skipped before page navigation"
            self.navigation.append(request.intent)
            self.safe_next = False
            return await self.workflow_state(sid)
        assert self.stage == "job_detail" and request.intent == "start_application"
        self.navigation.append(request.intent)
        self.stage = "auth_required"
        return await self.workflow_state(sid)

    async def pre_submit_check(self, sid):
        snapshot = await self.snapshot_for(sid)
        missing = [RequiredFieldIssue(selector=f.selector, label=f.label, field_type=f.field_type)
                   for f in snapshot.fields if f.required and not f.current_value]
        return PreSubmitCheck(url=self.page.url, ready=not missing, required_total=len(snapshot.fields),
                              filled_count=len(snapshot.fields)-len(missing), required_missing=missing,
                              submit_labels=["提交申请"])

    async def expandable_sections(self, sid):
        return []

    async def execute(self, sid, request, *args, **kwargs):
        assert sid == SID and self.stage == "application_form"
        assert not args and not request.resume_id and not request.upload_resume
        guard = kwargs.pop("before_action", lambda: None)
        assert not kwargs
        results = []
        for action in request.actions:
            guard()
            assert action.selector in self.values, "Password, declaration, submission or invented selector reached executor"
            assert action.action == "fill" and action.confidence >= .85
            self.values[action.selector] = str(action.value)
            results.append(ActionResult(selector=action.selector, label=action.label, status="filled", verified=True,
                                        actual_value=str(action.value), message="合成页面真实回读一致"))
        self.executions.append(request.model_copy(deep=True))
        return ExecutionResult(url=self.page.url, completed=len(results), skipped=0, failed=0, verified=len(results),
                               results=results, pre_submit=await self.pre_submit_check(sid))

    async def import_resume_with_site_parser(self, *args, **kwargs):
        raise AssertionError("Journey must never implicitly upload or parse an attachment")


def run():
    browser = SyntheticBrowser()
    orchestration_calls, mapping_calls = [], []

    async def workflow_model(agent, context, **kwargs):
        content = json.loads(context)
        orchestration_calls.append(content)
        action = content["allowed_next_actions"][0]
        return SimpleNamespace(final_output=ApplicationAgentDecision(
            stage=content["authoritative_stage"], goal="合成模型推进当前安全阶段", summary="按网页证据执行下一安全步",
            next_action=action, next_label="继续当前阶段", rationale="仅从代码允许的动作中选择"))

    async def field_model(snapshot, profile, *args):
        mapping_calls.append(snapshot)
        assert {field.selector for field in snapshot.fields} == {"#extra"}
        assert profile.email == "candidate@example.test"
        # The model proposes a path but invents a deliberately wrong value.
        # Real merge_grounded_plan must replace it with the stored source.
        return FormPlan(actions=[FillAction(selector="#extra", label="通知接收地址", action="fill",
            value="untrusted-model-value@example.test", profile_path="email", question_evidence="本人电子邮箱",
            confidence=.99, reason="根据网页上下文识别本人邮箱")])

    with patch("dotenv.load_dotenv"):
        from app import main
    with TemporaryDirectory(prefix="zhida-journey-fixture-") as temp, \
         patch.object(storage, "DATA_DIR", Path(temp)), \
         patch.object(storage, "DB_PATH", Path(temp)/"fixture.sqlite3"), \
         patch.object(main, "browser_demo", browser), \
         patch.dict(main.browser_session_owners, {SID:USER}, clear=True), \
         patch.dict(main.browser_task_resumes, {SID:RID}, clear=True), \
         patch.dict(main.browser_task_epochs, {SID:"fixture-epoch"}, clear=True), \
         patch.object(main, "_selected_resume_path", side_effect=AssertionError("No implicit attachment access")), \
         patch.object(application_agent, "configured_model", return_value=(object(), object())), \
         patch.object(application_agent, "Agent", return_value=object()), \
         patch.object(application_agent.Runner, "run", side_effect=workflow_model), \
         patch.object(form_agent, "configured_model", return_value=(None, None)), \
         patch.object(form_agent, "_structured_plan", side_effect=field_model), \
         patch.dict("os.environ", {"APP_AGENT_MODEL":"synthetic-model", "APP_AGENT_PROMPT_JSON_MODELS":""}), \
         patch("app.application_knowledge._embed", side_effect=AssertionError("No network embeddings")), \
         patch("socket.create_connection", side_effect=AssertionError("No network")):
        storage.initialize()
        token = storage.set_current_user(USER)
        try:
            storage.get_profile()  # As normal user activation does before edits.
            storage.save_profile(CandidateProfile(name="合成候选人", email="candidate@example.test"))
            profile = ResumeProfile(projects=[Project(name="合成 Agent 项目", start_date="2026-02")])
            for rid in (RID, "different-cv-fixture"):
                storage.create_resume(rid, "fixture.pdf", "fixture.pdf", rid, profile, "fixture", "中文", 10,
                    "synthetic-content-hash", "synthetic original text", [FieldEvidence(id="projects-fixture",
                    field_path="projects", value=[p.model_dump() for p in profile.projects], confidence=1,
                    source_text="合成附件摘录，未提供项目角色", status="confirmed")])

            fixture = FastAPI()
            @fixture.middleware("http")
            async def isolated_user(request, call_next):
                tok = storage.set_current_user(USER)
                try:
                    return await call_next(request)
                finally:
                    storage.reset_current_user(tok)
            fixture.add_api_route("/api/browser/{session_id}/journey", main.continue_application_journey,
                                  methods=["POST"], response_model=ApplicationJourneyResult)
            fixture.add_api_route("/api/resumes/{resume_id}/fact-targets", main.resume_fact_targets,
                                  methods=["GET"], response_model=FactTargets)
            fixture.add_api_route("/api/resumes/{resume_id}/confirmed-fact", main.confirm_resume_fact,
                                  methods=["POST"], response_model=ResumeRecord)
            with TestClient(fixture) as client:
                def proceed(resume_id=RID):
                    response = client.post(f"/api/browser/{SID}/journey", json={"resume_id":resume_id,"max_steps":4})
                    assert response.status_code == 200, response.text
                    return response.json()

                entered = proceed()
                assert entered["status"] == "waiting_login", entered
                assert entered["turn"]["action_taken"] == "start_application"
                assert browser.navigation == ["start_application"] and not browser.executions
                again = proceed()
                assert again["status"] == "waiting_login" and not browser.executions
                # Simulate human-only registration/OTP observations, not bypass.
                for stage, expected in (("registration_required", "waiting_registration"),
                                        ("verification_required", "waiting_verification")):
                    browser.stage = stage
                    assert proceed()["status"] == expected and not browser.executions

                # The human logs in externally. The same product button now
                # proceeds through the real step, assist and grounded mapper.
                browser.stage = "application_form"
                browser.sync_url()
                first = proceed()
                assert first["status"] == "needs_user", (first["status"], first["message"], first["turn"]["assistance"]["events"])
                assert first["turn"]["action_taken"] == "analyze_and_fill"
                assert browser.values["#name"] == "合成候选人"
                assert browser.values["#extra"] == "candidate@example.test"
                assert browser.values["#project-role"] == ""
                assert mapping_calls and orchestration_calls
                review = first["turn"]["review"]
                role = next(action for action in review["plan"]["actions"] if action["selector"] == "#project-role")
                assert role["action"] == "ask_user" and "项目角色" in role["label"]
                assert any(event["kind"] == "model" for event in first["turn"]["assistance"]["events"])
                assert all(a.value != "untrusted-model-value@example.test" for request in browser.executions for a in request.actions)

                # User responds in Zhida, not a script-only/browser-only answer.
                targets = client.get(f"/api/resumes/{RID}/fact-targets").json()
                record = next(item for item in targets["records"] if "合成 Agent 项目" in item["label"])
                saved = client.post(f"/api/resumes/{RID}/confirmed-fact", json={"revision":targets["revision"],
                    "record_key":record["record_key"], "attribute":"role", "value":"核心开发", "confirmed":True})
                assert saved.status_code == 200, saved.text
                assert saved.json()["profile"]["projects"][0]["role"] == "核心开发"
                resumed = proceed()
                assert resumed["status"] == "ready_for_review", resumed
                assert browser.values["#project-role"] == "核心开发"
                assert [item["selector"] for item in resumed["turn"]["pre_submit"]["required_missing"]] == ["#declaration"]
                assert not resumed["turn"]["pre_submit"]["ready"]  # Truthfulness is still human-owned.
                assert len(storage.confirmed_resume_fact_history(RID)) == 1
                count = len(browser.executions)
                # Retrying at a legal gate may re-evaluate an ambiguous field,
                # but must not rewrite already verified personal information.
                retried = proceed()
                assert retried["status"] in {"ready_for_review", "needs_user"}
                assert not [a for request in browser.executions[count:] for a in request.actions]

                # Same CV at a different company consumes the true fact without
                # carrying site-specific options, preserving the original PDF.
                browser.host = "company-b.example.test"
                browser.reset_form(); browser.sync_url()
                another_company = proceed()
                assert another_company["status"] == "ready_for_review", another_company
                assert browser.values["#project-role"] == "核心开发"
                assert storage.get_resume_internal(RID)["raw_text"] == "synthetic original text"
                browser.reset_form()
                main.browser_task_resumes[SID] = "different-cv-fixture"
                other_cv = proceed("different-cv-fixture")
                assert other_cv["status"] == "needs_user" and browser.values["#project-role"] == ""
                assert all(action.selector != "#declaration" for request in browser.executions for action in request.actions)
                assert browser.navigation == ["start_application"]

                # Optional unknowns are not completion. Even with the site's
                # required-fields check passing and no safe action at all,
                # the actual step must return a review for the UI to ask from.
                main.browser_task_resumes[SID] = RID
                browser.include_declaration = browser.include_model_field = False
                browser.optional_unknown = True
                browser.values["#name"] = "合成候选人"
                browser.values["#project-role"] = "核心开发"
                before = len(browser.executions)
                human_only = proceed()
                assert human_only["status"] == "needs_user", human_only["message"]
                assert human_only["turn"]["pre_submit"]["ready"]
                assert human_only["turn"]["action_taken"] == "" and human_only["turn"]["assistance"] is None
                assert len(browser.executions) == before
                questions = human_only["turn"]["review"]["plan"]["actions"]
                assert any(a["selector"] == "#optional-unknown" and a["action"] == "ask_user"
                           and "紧急联系人姓名" in a["label"] for a in questions)

                # The identical human-only question on an intermediate page
                # must not be silently discarded simply because Next is shown.
                browser.safe_next = True
                human_between_pages = proceed()
                assert human_between_pages["status"] == "needs_user"
                assert not human_between_pages["turn"]["action_taken"]
                assert browser.navigation == ["start_application"]
                # A current form without that question can advance normally.
                # This simulates a fresh conditional form, not invented facts
                # or hidden acceptance of the unknown optional answer.
                browser.optional_unknown = False
                completed = proceed()
                assert completed["status"] == "ready_for_review", completed["message"]
                assert browser.navigation == ["start_application", "continue_application"]
        finally:
            storage.reset_current_user(token)
    print("application_journey_product_test: OK (real journey→step→assist→grounded model→confirmed fact→reuse→human final gate; isolated I/O)")


if __name__ == "__main__":
    run()
