"""Exercise the actual Agents function tool with no model/network request."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import ModelSettings, RunContextWrapper
from agents.items import ItemHelpers
from app import form_agent
from app.browser_models import BrowserSnapshot, FillAction, FormPlan, PageField
from app.models import CandidateProfile
from app.page_observation import PageRegionObservation


def fixture():
    birth = PageField(selector='#birth', label='出生日期', question_text='出生日期',
        label_source='explicit', field_type='combobox', control_kind='calendar',
        container_key='personal-record', required=True)
    name = PageField(selector='#name', label='姓名', question_text='姓名',
        label_source='explicit', field_type='text', container_key='personal-record')
    return BrowserSnapshot(session_id='anonymous', url='https://fixture.example.test/form',
        title='Anonymous', fields=[birth, name]), CandidateProfile(name='匿名候选人', birth_date='2001-02-03')


async def run():
    snapshot, profile = fixture()
    observed = snapshot.fields[0].model_copy(update={'date_precision':'date',
        'control_evidence':'已展开唯一所属日历，要求年月日'})
    observer = AsyncMock(return_value=[observed])

    async def fake_run(agent, prompt, max_turns):
        assert max_turns == 4
        assert len(agent.tools) == 1 and agent.tools[0].name == 'inspect_controls'
        tool = agent.tools[0]
        context = RunContextWrapper(context=None)
        parsed = json.loads(prompt)
        assert [f['selector'] for f in parsed['page']['fields']] == ['#birth']
        assert [f['selector'] for f in parsed['page']['context_fields']] == ['#name']
        # Context neighbours, unknown selectors, and excess requests are not
        # executable targets and cannot cause any browser calls.
        for selectors in (['#name'], ['#submit'], ['#birth']*5):
            response = await tool.on_invoke_tool(context, json.dumps({'selectors':selectors}))
            assert 'error' in json.loads(response)
        response = await tool.on_invoke_tool(context, json.dumps({'selectors':['#birth']}))
        assert json.loads(response)['observations'][0]['date_precision'] == 'date'
        repeat = await tool.on_invoke_tool(context, json.dumps({'selectors':['#birth']}))
        assert 'error' in json.loads(repeat)
        return SimpleNamespace(final_output=FormPlan(actions=[FillAction(selector='#birth',
            label='出生日期', action='select', value='invented date', confidence=.99,
            profile_path='birth_date', question_evidence='出生日期')]))

    env = {'APP_AGENT_MODEL':'fixture', 'APP_AGENT_FALLBACK_MODEL':'', 'APP_AGENT_PROMPT_JSON_MODELS':''}
    with patch.dict('os.environ', env), \
            patch.object(form_agent, 'configured_model', return_value=('fixture', ModelSettings())), \
            patch.object(form_agent.Runner, 'run', side_effect=fake_run):
        result = await form_agent.create_form_plan(snapshot, profile, observe_controls=observer)
    observer.assert_awaited_once_with(['#birth'])
    action = next(a for a in result.actions if a.selector == '#birth')
    assert action.action == 'select' and action.value == profile.birth_date
    assert snapshot.fields[0].date_precision == 'date'
    assert next(a for a in result.actions if a.selector == '#name').value == profile.name

    # If the tool observes a changed question/record, stop even if a model
    # could retry or produce a high-confidence answer on the old JSON.
    snapshot, profile = fixture()
    changed = snapshot.fields[0].model_copy(update={'question_text':'另一人的出生日期'})
    observer = AsyncMock(return_value=[changed])
    env['APP_AGENT_FALLBACK_MODEL'] = 'fallback-fixture'
    async def moved_run(agent, prompt, max_turns):
        return await agent.tools[0].on_invoke_tool(RunContextWrapper(context=None),
            json.dumps({'selectors':['#birth']}))
    with patch.dict('os.environ', env), \
            patch.object(form_agent, 'configured_model', return_value=('fixture', ModelSettings())), \
            patch.object(form_agent.Runner, 'run', side_effect=moved_run) as runner:
        try:
            await form_agent.create_form_plan(snapshot, profile, observe_controls=observer)
            raise AssertionError('continued with changed question')
        except ValueError as exc:
            assert '题目或归属发生变化' in str(exc)
    assert runner.await_count == 1, 'must not invoke fallback on a changed page'
    await region_tools()
    print('form_observation_tool_test: OK (real SDK tools, bounded reads, multimodal/JSON paths, grounded values, stale-record stop)')


async def region_tools():
    """Verify SDK image transport without any browser, model or personal data."""
    for prompt_json in (False, True):
        snapshot, profile = fixture()
        for number in (2, 3):
            snapshot.fields.append(snapshot.fields[0].model_copy(update={
                'selector': '#birth' + str(number), 'container_key': 'fixture-' + str(number)}))
        # A small anonymous image payload stands in for the masked crop. This
        # test exercises SDK serialization, not image parsing or OS capture.
        image_url = 'data:image/png;base64,Zml4dHVyZQ=='

        async def observe_region(selector):
            return PageRegionObservation(selector=selector, accessibility='- combobox "出生日期"',
                image_data_url=image_url, context={'context_only': True})

        observer = AsyncMock(side_effect=observe_region)

        async def fake_run(agent, prompt, max_turns):
            assert max_turns == 7
            tool = next(t for t in agent.tools if t.name == 'inspect_page_region')
            context = RunContextWrapper(context=None)
            for selector in ('#name', '#submit'):
                denied = await tool.on_invoke_tool(context, json.dumps({'selector': selector}))
                assert isinstance(denied, str)
            for selector in ('#birth', '#birth2'):
                output = await tool.on_invoke_tool(context, json.dumps({'selector': selector}))
                wire = ItemHelpers._convert_tool_output(output)
                assert isinstance(wire, list)
                assert [part['type'] for part in wire] == ['input_text', 'input_image']
                assert wire[1]['image_url'] == image_url
                metadata = json.loads(wire[0]['text'])
                assert metadata['selector'] == selector
                assert 'image_data_url' not in metadata
            for selector in ('#birth', '#birth3'):
                denied = await tool.on_invoke_tool(context, json.dumps({'selector': selector}))
                assert isinstance(denied, str), 'repeat and excess region must not read browser'
            output = FormPlan(actions=[])
            return SimpleNamespace(final_output=output.model_dump_json() if prompt_json else output)

        env = {'APP_AGENT_MODEL': 'fixture', 'APP_AGENT_FALLBACK_MODEL': '',
               'APP_AGENT_PROMPT_JSON_MODELS': 'fixture' if prompt_json else ''}
        with patch.dict('os.environ', env), \
                patch.object(form_agent, 'configured_model', return_value=('fixture', ModelSettings())), \
                patch.object(form_agent.Runner, 'run', side_effect=fake_run):
            result = await form_agent.create_form_plan(snapshot, profile, observe_page_region=observer)
        assert [call.args[0] for call in observer.await_args_list] == ['#birth', '#birth2']
        assert image_url not in result.model_dump_json()
        assert image_url not in snapshot.model_dump_json()

    snapshot, profile = fixture()
    observer = AsyncMock(return_value=PageRegionObservation(selector='#another-record'))

    async def changed_run(agent, prompt, max_turns):
        tool = next(t for t in agent.tools if t.name == 'inspect_page_region')
        return await tool.on_invoke_tool(RunContextWrapper(context=None),
            json.dumps({'selector': '#birth'}))

    env['APP_AGENT_FALLBACK_MODEL'] = 'fallback-fixture'
    env['APP_AGENT_PROMPT_JSON_MODELS'] = ''
    with patch.dict('os.environ', env), \
            patch.object(form_agent, 'configured_model', return_value=('fixture', ModelSettings())), \
            patch.object(form_agent.Runner, 'run', side_effect=changed_run) as runner:
        try:
            await form_agent.create_form_plan(snapshot, profile, observe_page_region=observer)
            raise AssertionError('continued with a different observed target')
        except ValueError as exc:
            assert '目标不一致' in str(exc)
    assert runner.await_count == 1, 'wrong region target must not invoke the fallback model'


if __name__ == '__main__':
    asyncio.run(run())
