"""Anonymous, pure-memory guards for bounded control observation.

No browser is launched, no network/model is called, and storage access raises.
The preview stub only changes metadata, so each failure identifies an outer
guard rather than a dropdown/calendar/region executor implementation detail.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.application_models import ApplicationWorkflowState
from app.browser_models import BrowserSnapshot, PageField
from app.browser_service import BrowserDemoService
from app.form_observation import annotate_fields, merge_observed_metadata, model_page


URL = "https://anonymous.invalid/application"
SESSION = "anonymous-control-observation"
TARGET = '[data-anonymous-field="target"]'
OTHER = '[data-anonymous-field="other"]'


def owned_field(selector=TARGET, **changes):
    field = PageField(
        selector=selector, label="匿名选择题", question_text="匿名选择题",
        field_type="combobox", control_kind="select", container_key="anonymous-owner",
        label_source="container-owned", question_candidates=[
            {"text": "匿名选择题", "source": "container-owned", "owned": True}],
    )
    return PageField.model_validate({**field.model_dump(), **changes})


def snapshot(*fields):
    copied = [field.model_copy(deep=True) for field in fields]
    annotate_fields(copied)
    return BrowserSnapshot(session_id=SESSION, url=URL, title="匿名申请表", fields=copied)


def workflow(**changes):
    return ApplicationWorkflowState(
        session_id=SESSION, url=URL, title="匿名申请表", stage="application_form",
        job_id="anonymous-job-a", job_title="匿名岗位甲", page_step_current=1,
    ).model_copy(update=changes, deep=True)


CASCADE = {
    "control_kind": "cascade", "read_only": True,
    "scope": "owned_current_visible_layers", "options_capture": "dependent",
    "observed_layer_count": 2, "complete": False, "truncated": False,
    "limitations": ["仅观察已显示的匿名层，不选择或推断完整路径"],
    "layers": [
        {"visible_layer_index": 0, "declared_level": 1, "visible_option_count": 2,
         "truncated": False, "options": [
             {"text": "匿名省甲", "text_truncated": False, "disabled": False, "branch": True},
             {"text": "匿名省乙", "text_truncated": False, "disabled": True, "branch": True}]},
        {"visible_layer_index": 1, "declared_level": 2, "visible_option_count": 1,
         "truncated": False, "options": [
             {"text": "匿名市甲", "text_truncated": False, "disabled": False, "branch": False}]},
    ],
}


class ControlObservationGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_guard = patch("app.storage._connection", side_effect=AssertionError("no personal DB"))
        self.network_guard = patch("socket.create_connection", side_effect=AssertionError("no network"))
        self.db_guard.start()
        self.network_guard.start()
        self.addCleanup(self.db_guard.stop)
        self.addCleanup(self.network_guard.stop)

    def service(self, before, after=None, initial_workflow=None, after_workflow=None, metadata=None):
        service = BrowserDemoService()
        document = SimpleNamespace(evaluate=AsyncMock(return_value=True), dispose=AsyncMock())
        locator = SimpleNamespace(
            count=AsyncMock(return_value=1), is_enabled=AsyncMock(return_value=True),
            is_visible=AsyncMock(return_value=True), evaluate=AsyncMock(return_value=False),
        )
        service.session_id = SESSION
        service.page = SimpleNamespace(
            url=URL, locator=lambda selector: locator,
            evaluate_handle=AsyncMock(return_value=document),
        )
        preview_seen = False
        async def capture(*args, **kwargs):
            return (after if preview_seen and after is not None else before).model_copy(deep=True)
        async def inspect(*args, **kwargs):
            current = (after_workflow if preview_seen and after_workflow is not None
                       else initial_workflow or workflow())
            return current.model_copy(deep=True)
        async def preview(item, control, policy):
            nonlocal preview_seen
            preview_seen = True
            if metadata:
                item.update(metadata)
        service.snapshot = AsyncMock(side_effect=capture)
        service.workflow_state = AsyncMock(side_effect=inspect)
        service._assert_snapshot_document = AsyncMock()
        service._preview_field_options = AsyncMock(side_effect=preview)
        service._dismiss_options = AsyncMock()
        return service

    async def observe(self, service, expected, expected_state=None):
        return await service.observe_form_controls(
            SESSION, [TARGET], expected_snapshot=expected,
            expected_workflow=expected_state or workflow(),
        )

    async def test_unrequested_field_value_change_is_not_read_only(self):
        a = owned_field()
        b = owned_field(OTHER, field_type="text", control_kind="text", current_value="匿名原值")
        before = snapshot(a, b)
        after = snapshot(a, b.model_copy(update={"current_value": ""}))
        service = self.service(before, after)
        with self.assertRaises(ValueError):
            await self.observe(service, before)
        self.assertEqual(service._preview_field_options.await_count, 1)

    async def test_same_url_job_or_step_change_stops_old_observation(self):
        before = snapshot(owned_field())
        for changes in ({"job_id": "anonymous-job-b"}, {"page_step_current": 2}):
            with self.subTest(changes=changes):
                service = self.service(before, before, after_workflow=workflow(**changes))
                with self.assertRaises(ValueError):
                    await self.observe(service, before)
                self.assertEqual(service._preview_field_options.await_count, 1)

    async def test_batch_first_preview_changes_second_type_second_never_opens(self):
        a, b = owned_field(), owned_field(OTHER)
        before = snapshot(a, b)
        after = snapshot(a, b.model_copy(update={"field_type": "submit"}))
        service = self.service(before, after)
        with self.assertRaises(ValueError):
            await service.observe_form_controls(
                SESSION, [TARGET, OTHER], expected_snapshot=before, expected_workflow=workflow(),
            )
        self.assertEqual(service._preview_field_options.await_count, 1)
        self.assertEqual([call.args[0]["selector"] for call in service._preview_field_options.await_args_list], [TARGET])

    async def test_batch_first_preview_changes_step_second_never_opens(self):
        before = snapshot(owned_field(), owned_field(OTHER))
        service = self.service(before, before, after_workflow=workflow(page_step_current=2))
        with self.assertRaises(ValueError):
            await service.observe_form_controls(
                SESSION, [TARGET, OTHER], expected_snapshot=before, expected_workflow=workflow(),
            )
        self.assertEqual(service._preview_field_options.await_count, 1)
        self.assertEqual([call.args[0]["selector"] for call in service._preview_field_options.await_args_list], [TARGET])

    async def test_stale_expected_question_rejected_before_preview(self):
        expected = snapshot(owned_field())
        new_question = owned_field(
            label="匿名新题", question_text="匿名新题", question_candidates=[
                {"text": "匿名新题", "source": "container-owned", "owned": True}],
        )
        service = self.service(snapshot(new_question))
        with self.assertRaises(ValueError):
            await self.observe(service, expected)
        self.assertEqual(service._preview_field_options.await_count, 0)

    async def test_stale_expected_workflow_rejected_before_preview(self):
        before = snapshot(owned_field())
        service = self.service(before, initial_workflow=workflow(job_id="anonymous-job-b"))
        with self.assertRaises(ValueError):
            await self.observe(service, before, workflow())
        self.assertEqual(service._preview_field_options.await_count, 0)

    async def test_unverified_unknown_or_conflicting_source_never_opens(self):
        cases = [
            owned_field(label_source="generated", question_candidates=[]),
            owned_field(control_kind="unknown"),
            owned_field(question_candidates=[
                {"text": "匿名选择题", "source": "container-owned", "owned": True},
                {"text": "匿名冲突题", "source": "container-owned", "owned": True}]),
        ]
        for field in cases:
            with self.subTest(kind=field.control_kind, source=field.label_source):
                before = snapshot(field)
                service = self.service(before)
                with self.assertRaises(ValueError):
                    await self.observe(service, before)
                self.assertEqual(service._preview_field_options.await_count, 0)

    async def test_readonly_calendar_can_observe_format_without_value_write(self):
        calendar = owned_field(readonly=True, control_kind="calendar", current_value="")
        before = snapshot(calendar)
        service = self.service(before, before, metadata={"date_precision": "month"})
        samples = await self.observe(service, before)
        self.assertEqual(service._preview_field_options.await_count, 1)
        self.assertEqual(samples[0].date_precision, "month")
        self.assertTrue(samples[0].readonly)
        self.assertEqual(samples[0].current_value, "")
        self.assertEqual(samples[0].options, [])
        self.assertEqual(samples[0].observation.options_status, "calendar")

    async def test_model_caller_keeps_immutable_guard_across_two_metadata_reads(self):
        # The planner enriches its own snapshot after the first tool read.
        # That enrichment must not become a false DOM change on the next read.
        from app import main

        before = snapshot(owned_field(), owned_field(OTHER))
        service = self.service(before, before, metadata={
            "options": ["匿名候选甲", "匿名候选乙"], "options_capture": "observed_subset",
        })
        sentinel_plan = object()
        async def mapper(caller_snapshot, profile, *, observe_controls, **kwargs):
            first = await observe_controls([TARGET])
            merge_observed_metadata(caller_snapshot, first)
            self.assertEqual(caller_snapshot.fields[0].options, ["匿名候选甲", "匿名候选乙"])
            second = await observe_controls([OTHER])
            merge_observed_metadata(caller_snapshot, second)
            self.assertEqual(caller_snapshot.fields[1].options, ["匿名候选甲", "匿名候选乙"])
            return sentinel_plan
        with patch.object(main, "browser_demo", service), \
                patch.object(main, "_task_context", return_value=(object(), "anonymous-revision")), \
                patch.object(main, "_require_browser_owner"), \
                patch.object(main, "create_form_plan", side_effect=mapper), \
                patch.object(main, "_stamp_plan", side_effect=lambda session, plan: plan):
            actual = await main._model_task_plan(SESSION, before.model_copy(deep=True))
        self.assertIs(actual, sentinel_plan)
        self.assertEqual(service._preview_field_options.await_count, 2)

    async def test_cascade_evidence_stays_typed_and_never_becomes_flat_options(self):
        cascade = owned_field(readonly=True, control_kind="cascade", current_value="匿名既有路径")
        before = snapshot(cascade)
        service = self.service(before, before, metadata={
            "cascade_observation": CASCADE, "options": [], "options_capture": "dependent",
        })
        samples = await self.observe(service, before)
        sample = samples[0]
        self.assertEqual(service._preview_field_options.await_count, 1)
        self.assertEqual(sample.options, [])
        self.assertEqual(sample.current_value, "匿名既有路径")
        self.assertEqual(sample.observation.options_status, "dependent")
        self.assertEqual(sample.cascade_observation.observed_layer_count, 2)
        self.assertFalse(sample.cascade_observation.complete)
        self.assertTrue(sample.cascade_observation.layers[0].options[1].disabled)
        merged = before.model_copy(deep=True)
        merge_observed_metadata(merged, samples)
        encoded = model_page(merged)
        for representation in (encoded["fields"][0], encoded["questions"][0]):
            self.assertEqual(representation["options"], [])
            self.assertEqual(representation["cascade_observation"]["observed_layer_count"], 2)
            self.assertFalse(representation["cascade_observation"]["complete"])
            self.assertEqual(len(representation["cascade_observation"]["layers"]), 2)
        sample.cascade_observation.layers[0].options[0].text = "匿名后续观察"
        self.assertEqual(merged.fields[0].cascade_observation.layers[0].options[0].text, "匿名省甲")


if __name__ == "__main__":
    unittest.main(verbosity=2)
