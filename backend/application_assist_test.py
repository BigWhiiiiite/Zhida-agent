"""Product preparation loop tests: synthetic browser, no network/database/PII."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from app import application_assist as assist
from app.application_models import ApplicationWorkflowState
from app.browser_models import (ActionResult, ApplicationAssistRequest, BrowserSnapshot,
    ExecutionResult, FillAction, FormPlan, NativeResumeImportResult, PageField, PreSubmitCheck,
    RequiredFieldIssue)
from app.form_agent import _local_safe_plan
from app.form_routing import route_local_plan
from app.models import CandidateProfile, Project


URL = "https://fixture.example.test/application"


class Browser:
    def __init__(self, projects=False):
        self.values = {"name": "", "country": "", "city": ""}
        self.groups = []
        self.projects = projects
        self.calls = []
        self.expansions = 0
        self.uploads = 0
        self.stage = "application_form"
        self.options_loaded = False
        self.fail = False
        self.change_context = False

    async def workflow_state(self, sid):
        return ApplicationWorkflowState(session_id=sid, url=URL, title="Fixture", stage=self.stage,
                                        form_fields=4, final_submit_present=True)

    async def settled_snapshot(self, sid):
        fields = [PageField(selector="#name", label="姓名", question_text="姓名", label_source="explicit",
                      required=True, semantic_key="candidate.name", current_value=self.values["name"]),
            PageField(selector="#country", label="国家/地区", question_text="国家/地区", label_source="explicit",
                      required=True, field_type="combobox", semantic_key="candidate.country_region",
                      current_value=self.values["country"], options=["中国"]),
            PageField(selector="#city"+str(self.options_loaded), label="意向城市", question_text="意向城市",
                      label_source="explicit", required=True, field_type="combobox", semantic_key="preference.work_location",
                      current_value=self.values["city"], options=["北京"] if self.options_loaded else []),
            PageField(selector="#agree", label="我承诺资料真实并承担法律责任", field_type="checkbox",
                      current_value="false", required=True, label_source="explicit")]
        for i, group in enumerate(self.groups):
            for key, label in (("name", "项目名称"), ("role", "项目角色"), ("description", "项目描述")):
                fields.append(PageField(selector=f"#project{i}-{key}", label=label, question_text=label,
                    semantic_key="project."+key, container_key=f"p:{i}", section="项目经历", label_source="explicit",
                    field_type="textarea" if key=="description" else "text", required=True, current_value=group[key]))
        return BrowserSnapshot(session_id=sid, url=URL, title="Fixture", fields=fields)

    async def expandable_sections(self, sid):
        return ([dict(id=f"p{len(self.groups)}", selector="#add", kind="projects", record_count=len(self.groups),
                      container_key="p", label="项目经历", record_keys=[f"p:{i}" for i in range(len(self.groups))])]
                if self.projects else [])

    async def expand_missing_section(self, sid, candidate):
        assert candidate["record_count"] == len(self.groups)
        self.expansions += 1
        self.groups.append({"name": "", "role": "", "description": ""})
        return await self.settled_snapshot(sid)

    async def import_resume_with_site_parser(self, sid, path, confirm):
        assert confirm is True and path == Path("fixture.pdf")
        self.uploads += 1
        return NativeResumeImportResult(snapshot=await self.settled_snapshot(sid), uploaded_file="fixture.pdf",
                                        status="parsed", message="Synthetic parser completed")

    async def pre_submit_check(self, sid):
        snapshot = await self.settled_snapshot(sid)
        missing = [RequiredFieldIssue(selector=f.selector, label=f.label, field_type=f.field_type)
                   for f in snapshot.fields if f.required and (not f.current_value or f.current_value == "false")]
        return PreSubmitCheck(url=URL, required_total=len(snapshot.fields), filled_count=len(snapshot.fields)-len(missing),
                              required_missing=missing, ready=not missing, submit_labels=["提交申请"])

    async def execute(self, sid, request, *args, before_action=None):
        assert not args and not request.upload_resume and not request.resume_id
        self.calls.append(request)
        results = []
        loaded_before = self.options_loaded
        for action in request.actions:
            if before_action:
                before_action()
            assert action.selector != "#agree" and action.action in {"fill", "select"}
            failed = self.fail or (action.selector.startswith("#city") and not loaded_before)
            if not failed:
                if action.selector.startswith("#project"):
                    record, key = action.selector.removeprefix("#project").split("-")
                    self.groups[int(record)][key] = str(action.value)
                else:
                    key = "city" if action.selector.startswith("#city") else action.selector.removeprefix("#")
                    self.values[key] = str(action.value)
            results.append(ActionResult(selector=action.selector, label=action.label,
                status="failed" if failed else "filled", verified=not failed))
        if self.values["country"]:
            self.options_loaded = True  # dependent field gets a NEW selector
        return ExecutionResult(url=URL, completed=sum(r.verified for r in results), verified=sum(r.verified for r in results),
            failed=sum(not r.verified for r in results), skipped=0, results=results,
            pre_submit=await self.pre_submit_check(sid))


async def run():
    profile = CandidateProfile(name="合成测试人", country_region="中国", target_cities=["北京"], projects=[
        Project(name="项目甲唯一名称", role="核心开发", description="仅甲的内容"),
        Project(name="项目乙唯一名称", role="研究", description="仅乙的内容")])
    async def no_model(snapshot):
        raise AssertionError("known facts must not call model")
    def local(snapshot, candidate):
        return route_local_plan(_local_safe_plan(snapshot, candidate), snapshot, candidate)
    request = ApplicationAssistRequest(resume_id="fixture", use_model=False)

    async def prepare(browser, req=request, **extra):
        return await assist.prepare_application(browser, "fixture", req, profile,
            guard=lambda: None, model_plan=no_model, stamp_plan=lambda plan: plan,
            resume_path=Path("fixture.pdf"), **extra)

    with patch.object(assist, "create_local_form_plan", side_effect=local):
        browser = Browser(projects=True)
        result = await prepare(browser)
        assert result.status == "ready_for_review", result
        assert browser.expansions == 2 and browser.uploads == 0
        assert [g["name"] for g in browser.groups] == [p.name for p in profile.projects]
        assert browser.groups[0]["description"] == "仅甲的内容" and browser.groups[1]["description"] == "仅乙的内容"
        names = [a for call in browser.calls for a in call.actions if a.selector == "#name"]
        assert len(names) == 1  # dependent retry did not rewrite verified identity
        assert result.rounds == 2 and any(e.failed == 1 for e in result.events)
        assert len(result.pre_submit.required_missing) == 1
        before = len(browser.calls)
        again = await prepare(browser)
        assert again.status == "ready_for_review" and len(browser.calls) == before and browser.expansions == 2

        denied = Browser()
        pending = await prepare(denied, pending_sections=["项目经历"])
        assert pending.status == "needs_user" and not denied.calls and denied.uploads == 0
        login = Browser(); login.stage = "auth_required"
        assert (await prepare(login)).status == "blocked" and not login.calls
        failed = Browser(); failed.fail = True
        failure_result = await prepare(failed)
        assert failure_result.status == "partial" and len(failed.calls) == 1
        assert len(next(e for e in failure_result.events if e.kind == 'fill').issues) == 3

        # Unchanged failures must not prevent analysis of unrelated questions.
        model_calls = []
        async def model_after_failure(snapshot):
            model_calls.append(snapshot)
            return local(snapshot, profile)
        with patch.object(assist, 'create_local_form_plan', side_effect=lambda snapshot, candidate:
                          local(snapshot, candidate).model_copy(update={'routing_summary':
                              local(snapshot, candidate).routing_summary.model_copy(update={'model_pending': 1})})):
            all_failed = Browser(); all_failed.fail = True
            await assist.prepare_application(all_failed, 'fixture', request.model_copy(update={'use_model':True}),
                profile, guard=lambda:None, model_plan=model_after_failure, stamp_plan=lambda p:p)
            assert len(model_calls) == 1 and len(all_failed.calls) == 1
        uploaded = Browser()
        await prepare(uploaded, request.model_copy(update={"allow_site_parse": True}))
        assert uploaded.uploads == 1

        # An old unnamed nonempty project is never overwritten/duplicated by ordinal.
        ambiguous = Browser(projects=True)
        ambiguous.groups = [{"name":"不明旧项目", "role":"", "description":"旧内容"}]
        result = await prepare(ambiguous)
        assert result.status == "needs_user" and ambiguous.expansions == 0
        assert ambiguous.groups[0]["name"] == "不明旧项目"
    print("application_assist_test: OK (new-record anchors, dependency repair, no repeat writes/upload/submit, bounds, user gates)")


if __name__ == "__main__":
    asyncio.run(run())
