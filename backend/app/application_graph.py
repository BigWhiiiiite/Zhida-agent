"""Bounded form planning graph. Models map evidence, never operate the browser.

The graph deliberately has no persistence of applicant bodies to checkpoints
or LangSmith. Durable continuation lives at the D3 business round boundary.
"""
from typing import TypedDict
from langgraph.graph import START, END, StateGraph
from langsmith.run_helpers import tracing_context

from .form_agent import create_local_form_plan
from .form_field_policy import is_declaration
from .backend_v2.observation_adapter import adapt_browser_snapshot
from .backend_v2.form_recognition import normalize_observation, REASONS


class PlanningState(TypedDict, total=False):
    snapshot: object
    profile: object
    plan: object
    trace: list[str]


def validate_plan(snapshot, plan):
    """Fail closed on model-invented selectors, questions or action kinds."""
    fields = {f.selector:f for f in snapshot.fields}
    recognition=normalize_observation(adapt_browser_snapshot(snapshot))
    recognized={target['selector']:item for item in recognition['fields'] for target in item['targets']}
    for action in plan.actions:
        field = fields.get(action.selector)
        if not field or is_declaration(field) or field.field_type in {'submit','button','password','hidden'}:
            action.action, action.value, action.needs_model = 'skip','',False
            action.reason = '当前文档没有可执行的题目证据，或属于本人声明；不执行'
            continue
        if action.action not in {'fill','select','check','skip'}:
            action.action,action.value = 'skip',''
            action.reason = '不支持此操作；需要重新观察真实控件'
        # An answer mapping cannot promote an unclear label to a verified one.
        from .form_observation import extraction_block_reason
        blocker = extraction_block_reason(field)
        evidence=recognized.get(action.selector)
        if not evidence or not evidence['matchable']:
            blocker=blocker or '；'.join(REASONS.get(reason,reason) for reason in (evidence or {}).get('reasons',[])) or '未取得唯一完整的题目证据'
        if blocker and action.action!='skip':
            action.action,action.value = 'skip',''
            action.reason = blocker
            action.resolution_source='blocked'
    return plan


def create_graph_form_plan(snapshot, profile):
    graph = StateGraph(PlanningState)
    graph.add_node('read_and_understand',lambda s:{'trace':['read_and_understand']})
    graph.add_node('retrieve_confirmed_facts',lambda s:{'plan':create_local_form_plan(s['snapshot'],s['profile']),
        'trace':[*s['trace'],'retrieve_confirmed_facts']})
    graph.add_node('validate_answer_plan',lambda s:{'plan':validate_plan(s['snapshot'],s['plan']),
        'trace':[*s['trace'],'validate_answer_plan']})
    graph.add_edge(START,'read_and_understand');graph.add_edge('read_and_understand','retrieve_confirmed_facts')
    graph.add_edge('retrieve_confirmed_facts','validate_answer_plan');graph.add_edge('validate_answer_plan',END)
    with tracing_context(enabled=False):
        return graph.compile().invoke({'snapshot':snapshot,'profile':profile},config={'callbacks':[]})['plan']


async def create_graph_model_plan(snapshot, model_plan):
    async def analyse(s):
        return {'plan':await model_plan(s['snapshot']),'trace':[*s['trace'],'model_ambiguities']}
    graph = StateGraph(PlanningState)
    graph.add_node('read_evidence',lambda s:{'trace':['read_evidence']})
    graph.add_node('model_ambiguities',analyse)
    graph.add_node('validate_answer_plan',lambda s:{'plan':validate_plan(s['snapshot'],s['plan']),
        'trace':[*s['trace'],'validate_answer_plan']})
    graph.add_edge(START,'read_evidence');graph.add_edge('read_evidence','model_ambiguities')
    graph.add_edge('model_ambiguities','validate_answer_plan');graph.add_edge('validate_answer_plan',END)
    with tracing_context(enabled=False):
        return (await graph.compile().ainvoke({'snapshot':snapshot},config={'callbacks':[]}))['plan']
