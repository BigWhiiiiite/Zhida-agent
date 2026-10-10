"""Isolated browser service contracts: no network, dotenv, DB or live browser."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import browser_service
from app.application_models import ApplicationWorkflowState
from app.browser_models import BrowserSnapshot, ExecutePlanRequest, FillAction, PageField, PreSubmitCheck
from app.browser_service import BrowserDemoService


AUTOHOME = "https://talent.autohome.com.cn/recruit-delivery.html?pid=fixture"
UNKNOWN = "https://ats.example.test/application"
SESSION = "offline-assist-contract"


class FakePage:
    def __init__(self, url: str):
        self.url = url
        self.rendered_education = False
        self.document_origin = 1234
        self.wait_for_timeout = AsyncMock()
        self.evaluate = AsyncMock(side_effect=self._evaluate)

    async def evaluate_handle(self, script, argument=None):
        assert script == '() => document'
        return SimpleNamespace(
            evaluate=AsyncMock(return_value=True),
            dispose=AsyncMock(),
        )

    def _evaluate(self, script, argument=None):
        if script == "() => performance.timeOrigin":
            return self.document_origin
        if script == "origin => performance.timeOrigin === origin":
            return self.document_origin == argument
        if script == browser_service.NATIVE_WRITE_IDENTITIES:
            return {selector: {"native-question": selector} for selector in argument}
        return self.rendered_education

    def is_closed(self):
        return False


def field(name: str = "name", **kwargs):
    return PageField(selector="#" + name, name=name, label=name, semantic_key="candidate.name", **kwargs)


def snapshot(url=UNKNOWN, fields=None):
    return BrowserSnapshot(session_id=SESSION, url=url, title="Fixture", fields=fields or [field()])


def service_at(url=UNKNOWN):
    service = BrowserDemoService()
    service.session_id = SESSION
    service.page = FakePage(url)
    service.workflow_state = AsyncMock(side_effect=lambda session: ApplicationWorkflowState(
        session_id=session, url=service.page.url, title="Fixture", stage="application_form"))
    return service


async def readiness_tests():
    service = service_at()
    service.snapshot = AsyncMock(return_value=snapshot())
    with patch.object(browser_service.asyncio, "sleep", AsyncMock()):
        assert (await service.settled_snapshot(SESSION)).fields[0].name == "name"
    assert service.snapshot.await_count == 3
    assert [call.kwargs for call in service.snapshot.await_args_list] == [
        {"probe_options": False}, {"probe_options": False}, {"probe_options": False}]

    # Stable observation must never implicitly request dropdown discovery.
    service = service_at()
    clock = SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now

    async def probe(**kwargs):
        if kwargs.get('probe_options', True):
            clock.now = 6.0
        return snapshot()

    service.snapshot = AsyncMock(side_effect=probe)
    with patch.object(browser_service, 'time', clock), patch.object(browser_service.asyncio, 'sleep', AsyncMock()):
        assert (await service.settled_snapshot(SESSION)).fields
    assert clock.now == 0.0 and service.snapshot.await_count == 3

    # A new question during final passive verification invalidates the plan.
    service = service_at()
    service.snapshot = AsyncMock(side_effect=[snapshot(), snapshot(), snapshot(fields=[field('changed')])])
    with patch.object(browser_service.asyncio, 'sleep', AsyncMock()):
        try:
            await service.settled_snapshot(SESSION)
            raise AssertionError('changed question during option preview accepted')
        except ValueError as exc:
            assert '题目或记录归属已变化' in str(exc)

    # The known official form can have a stable personal shell before the
    # education template renders. Two equal shell samples are not completion.
    service = service_at(AUTOHOME)
    shell = snapshot(AUTOHOME, [field("shell" + str(i)) for i in range(9)])
    school = field("school").model_copy(update={
        "semantic_key": "education.school", "container_key": "autohome:education:1:1"})
    hydrated = snapshot(AUTOHOME, shell.fields + [school] + [field("edu" + str(i)) for i in range(8)])
    samples = [shell, shell, hydrated, hydrated, hydrated]

    async def hydrate(**kwargs):
        sample = samples.pop(0)
        service.page.rendered_education = sample is hydrated
        return sample

    service.snapshot = AsyncMock(side_effect=hydrate)
    with patch.object(browser_service.asyncio, "sleep", AsyncMock()):
        actual = await service.settled_snapshot(SESSION)
    assert len(actual.fields) == 18 and service.snapshot.await_count == 5

    # A template visible in DOM but absent from extracted fields is also partial.
    service = service_at(AUTOHOME)
    service.page.rendered_education = True
    service.snapshot = AsyncMock(return_value=shell)
    with patch.object(browser_service.asyncio, "sleep", AsyncMock()):
        try:
            await service.settled_snapshot(SESSION)
            raise AssertionError("partial education snapshot accepted")
        except ValueError as exc:
            assert "尚未稳定" in str(exc)
    assert service.snapshot.await_count == 8

    # Options are part of readiness; values alone do not fake stable options.
    service = service_at()
    choices = [[], ["北京"], ["北京", "上海"], ["北京", "上海"], ["北京", "上海"]]
    service.snapshot = AsyncMock(side_effect=[snapshot(fields=[field(options=values)]) for values in choices])
    with patch.object(browser_service.asyncio, "sleep", AsyncMock()):
        assert (await service.settled_snapshot(SESSION)).fields[0].options == choices[-1]
    assert service.snapshot.await_count == 5

    # No semantic personal field: a stable search control is not a form.
    service = service_at()
    service.snapshot = AsyncMock(return_value=snapshot(fields=[PageField(selector="#search", label="搜索岗位")]))
    with patch.object(browser_service.asyncio, "sleep", AsyncMock()):
        try:
            await service.settled_snapshot(SESSION)
            raise AssertionError("search-only page accepted")
        except ValueError:
            pass

    # Navigation while observing cannot return the old form's executable fields.
    service = service_at()

    async def navigate(**kwargs):
        service.page.url = "https://ats.example.test/login"
        return snapshot(service.page.url)

    service.snapshot = AsyncMock(side_effect=navigate)
    actual = await service.settled_snapshot(SESSION)
    assert actual.url.endswith("/login") and actual.fields == []
    assert service.snapshot.await_count == 1

    # Login can replace a form without changing the URL; return to stage gate.
    service = service_at()
    service.workflow_state.side_effect = [
        ApplicationWorkflowState(session_id=SESSION, url=UNKNOWN, title="Fixture", stage="application_form"),
        ApplicationWorkflowState(session_id=SESSION, url=UNKNOWN, title="Fixture", stage="auth_required"),
    ]
    service.snapshot = AsyncMock(return_value=snapshot())
    await service.settled_snapshot(SESSION)
    assert service.snapshot.await_count == 1

    # Slow page inspection is bounded too, not merely the polling sleep.
    service = service_at()
    service.snapshot = AsyncMock(side_effect=lambda: asyncio.Event().wait())
    # A coroutine side_effect must itself be async for AsyncMock to await it.
    async def blocked(**kwargs):
        await asyncio.Event().wait()
    service.snapshot.side_effect = blocked
    clock = SimpleNamespace(monotonic=lambda: next(clock_values))
    clock_values = iter([0.0, 4.99, 4.99])
    with patch.object(browser_service, "time", clock):
        try:
            await service.settled_snapshot(SESSION)
            raise AssertionError("unbounded slow snapshot")
        except ValueError as exc:
            assert "仍在加载" in str(exc)


async def expansion_tests():
    service = service_at(AUTOHOME)
    raw = {
        "id": "current-record-count-token", "selector": "#validated-add", "semantic_section": "experience",
        "record_count": 1, "container_key": "autohome:experience:3", "record_keys": ["autohome:experience:3:1"],
        "label": "增加实习/工作经历",
    }
    with patch.object(browser_service, "discover_autohome_sections", AsyncMock(return_value=[raw])) as discover:
        candidate = (await service.expandable_sections(SESSION))[0]
        assert candidate["kind"] == "internships" and candidate["record_keys"] == raw["record_keys"]
        current = snapshot(AUTOHOME)
        service.expand_section = AsyncMock(return_value=current)
        assert await service.expand_missing_section(SESSION, candidate) is current
        service.expand_section.assert_awaited_once_with(SESSION, "#validated-add", candidate_id=raw["id"])
        for change in ({"selector": "#submit"}, {"record_count": 0}, {"kind": "projects"}, {"record_keys": []}):
            try:
                await service.expand_missing_section(SESSION, {**candidate, **change})
                raise AssertionError("tampered expansion accepted")
            except ValueError:
                pass
        discover.return_value = [{**raw, "id": "new-token", "record_count": 2,
                                  "record_keys": [*raw["record_keys"], "autohome:experience:3:2"]}]
        try:
            await service.expand_missing_section(SESSION, candidate)
            raise AssertionError("stale expansion repeated")
        except ValueError:
            pass
        assert service.expand_section.await_count == 1
        discover.return_value = []
        assert await service.expandable_sections(SESSION) == []


async def legal_gate_tests():
    service = service_at()
    declaration = field("promise", field_type="checkbox").model_copy(update={
        "label": "我承诺所填简历真实可信，愿承担法律责任", "semantic_key": ""})
    gender = field("gender", field_type="radio").model_copy(update={"label": "性别", "option_label": "男"})
    preference = field("relocate", field_type="radio").model_copy(update={"label": "是否同意异地工作", "option_label": "是"})
    agreement = field("agreechk", field_type="checkbox").model_copy(update={"label": "", "semantic_key": ""})
    service.snapshot = AsyncMock(return_value=snapshot(fields=[declaration, gender, agreement, preference]))
    control = SimpleNamespace(evaluate=AsyncMock(return_value=True), set_checked=AsyncMock())
    service._resolve_field = AsyncMock(side_effect=lambda field: (field, control))
    service._read_field_value = AsyncMock(return_value="true")
    service.pre_submit_check = AsyncMock(return_value=PreSubmitCheck(url=UNKNOWN))
    actions = [
        FillAction(selector=declaration.selector, label=declaration.label, action="check", value=True,
                   confidence=1, user_confirmed=flag, sensitive=False) for flag in (False, True)
    ] + [
        FillAction(selector=agreement.selector, label="", action="check", value=True,
                   confidence=1, user_confirmed=True, sensitive=False),
        FillAction(selector=gender.selector, label="性别", action="check", value=True,
                   confidence=1, user_confirmed=True),
        FillAction(selector=preference.selector, label=preference.label, action="check", value=True,
                   confidence=1, user_confirmed=True),
    ]
    result = await service.execute(SESSION, ExecutePlanRequest(actions=actions))
    assert result.completed == 2 and result.verified == 2 and result.skipped == 3
    assert all("本人" in row.message for row in result.results[:3])
    assert {call.args[0].selector for call in service._resolve_field.await_args_list} == {gender.selector, preference.selector}
    assert control.set_checked.await_count == 2
    # Read-only document identity checks are allowed; no page-level script
    # may accept a declaration or bypass the skipped legal controls.
    assert all(call.args[0] in {
        "() => performance.timeOrigin", "origin => performance.timeOrigin === origin",
        browser_service.NATIVE_WRITE_IDENTITIES,
    } for call in service.page.evaluate.await_args_list)


async def run():
    await readiness_tests()
    await expansion_tests()
    await legal_gate_tests()
    await revision_gate_tests()
    print("browser_assist_capabilities_test: OK (bounded hydration, options, navigation/auth, exact expansion, no legal auto-consent)")


async def revision_gate_tests():
    service = service_at()
    first, second = field("first"), field("second")
    service.snapshot = AsyncMock(return_value=snapshot(fields=[first, second]))
    writes = []
    async def write(value, **kwargs):
        writes.append(value)
    control = SimpleNamespace(fill=AsyncMock(side_effect=write))
    service._resolve_field = AsyncMock(side_effect=lambda item: (item, control))
    service._read_field_value = AsyncMock(return_value="first-value")
    service.pre_submit_check = AsyncMock(return_value=PreSubmitCheck(url=UNKNOWN))
    def guard():
        if writes:
            raise ValueError("profile revision changed during first write")
    actions = [FillAction(selector=item.selector,label=item.label,action="fill",value=value,confidence=1)
               for item, value in ((first,"first-value"),(second,"second-value"))]
    try:
        await service.execute(SESSION,ExecutePlanRequest(actions=actions),before_action=guard)
        raise AssertionError("A changed revision did not stop the batch")
    except ValueError as exc:
        assert "revision changed" in str(exc)
    assert writes == ["first-value"]
    assert service._resolve_field.await_count == 1
    # Changes occurring inside the asynchronous resolver must also prevent
    # the very next write, not only stop at the start of the next field.
    writes.clear()
    def changed_after_resolution():
        if service._resolve_field.await_count:
            raise ValueError("changed while locating the control")
    service._resolve_field.reset_mock()
    try:
        await service.execute(SESSION,ExecutePlanRequest(actions=actions),before_action=changed_after_resolution)
        raise AssertionError("A changed revision after field resolution was accepted")
    except ValueError:
        pass
    assert not writes


if __name__ == "__main__":
    asyncio.run(run())
