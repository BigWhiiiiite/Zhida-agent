"""D2: bounded recognition/retrieval/planning graph, without browser writes.

Runner supplies an immutable, explicitly bound observation. Evidence processing
is pure; optional retrieval uses the existing local hybrid index. No external
LLM, screenshot upload, website calls, fill_plan or action_ledger insert occurs.
Detailed evidence remains in the RLS-protected report, never in checkpoints or
queue events. This is not the live workbench browser bridge yet.
"""
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from langgraph.graph import START, END, StateGraph
from sqlalchemy import text

from .observation_store import D2_VERSION, document_url
from .repository import digest, encode
from .workflow import DurableWorkflowEngine, WorkflowState
from .workers import StaleWork


class ReadOnlyWorkflowEngine(DurableWorkflowEngine):
    workflow_version = D2_VERSION
    nodes_per_advance = 6

    def __init__(self, db, *, clock=None, retriever=None):
        super().__init__(db, clock=clock)
        if retriever is None:
            from .rag import RagService
            retriever = RagService(db)
        self.retriever = retriever

    def read_authority(self, conn, user_id, run_id, *, lock=False):
        authority = super().read_authority(conn, user_id, run_id, lock=lock)
        run = authority['run']
        authority['application_url'] = conn.execute(text(
            'SELECT application_url FROM zhida.application_runs WHERE user_id=:u AND id=:r'),
            {'u': user_id, 'r': run_id}).scalar_one()
        observation = conn.execute(text('SELECT * FROM zhida.form_observations WHERE user_id=:u AND id=:o'),
            {'u': user_id, 'o': authority['observation_id']}).mappings().first()
        authority['observation'] = dict(observation) if observation else None
        facts = [dict(row) for row in conn.execute(text("""SELECT f.id,f.user_id,f.subject_id,f.semantic_key,f.scope_key,
            r.id AS revision_id,r.revision_no,r.value,r.source,r.confirmation,r.valid_until
            FROM zhida.profile_facts f JOIN zhida.fact_revisions r ON r.id=f.current_revision_id AND r.user_id=f.user_id
            WHERE f.user_id=:u
            ORDER BY f.id LIMIT 1001"""), {'u': user_id}).mappings()]
        for fact in facts:
            if fact['valid_until']:
                fact['valid_until'] = fact['valid_until'].isoformat()
        entities = [dict(row) for row in conn.execute(text("""SELECT e.id,e.entity_type,v.education_level,v.content
            FROM zhida.experience_entities e JOIN zhida.experience_versions v ON v.entity_id=e.id AND v.user_id=e.user_id
            WHERE e.user_id=:u AND v.resume_revision_id=:rev ORDER BY e.id LIMIT 201"""),
            {'u': user_id, 'rev': run['resume_revision_id']}).mappings()]
        authority['facts'], authority['entities'] = facts, entities
        active_refs = [f['revision_id'] for f in facts if f['confirmation'] == 'confirmed' and
                       (not f['valid_until'] or datetime.fromisoformat(f['valid_until']) > self.clock())]
        authority['fingerprint'] = digest([authority['fingerprint'], authority['application_url'],
            authority['observation'], facts, entities, active_refs])
        return authority

    def _observation_issue(self, state, authority):
        observed = authority.get('observation')
        if not observed:
            return 'observation_required'
        evidence = observed['evidence']
        if (evidence.get('schema_version') != 'runner-observation-v1' or
            evidence.get('browser_session_id') != state['browser_session_id'] or
            evidence.get('connection_epoch') != state['browser_connection_epoch'] or
            evidence.get('document_id') != observed['document_id']):
            return 'observation_binding_changed'
        try:
            if document_url(evidence['url']) != document_url(authority['application_url']):
                return 'observation_binding_changed'
            captured = datetime.fromisoformat(evidence['captured_at'])
            if captured.tzinfo is None or not -5 <= (self.clock() - captured).total_seconds() <= 300:
                return 'observation_expired'
        except (KeyError, ValueError, TypeError):
            return 'observation_binding_changed'
        if len(authority['facts']) > 1000 or len(authority['entities']) > 200:
            return 'analysis_context_limit'
        return None

    def evaluate(self, state, authority):
        from .answer_planning import build_answer_plan
        from .form_recognition import normalize_observation

        # All invocation-specific data lives in this closure, not self: parallel
        # calls cannot leak one applicant's report into another checkpoint.
        data = {}
        waiting = self._observation_issue(state, authority)
        context = {'user_id': state['user_id'], 'resume_revision_id': state['resume_revision_id'],
                   'site_scope': urlsplit(authority['application_url']).hostname,
                   'entities': authority['entities'], 'now': self.clock().isoformat()}

        def node(name, values=None):
            return lambda s: {**(values or {}), 'trace': [*s.get('trace', []), name]}

        def route(s):
            if s.get('block_reason'):
                return 'fail_closed'
            if s['unresolved_action_count']:
                return 'reconcile'
            if s['pending_confirmation_count']:
                return 'wait_for_user'
            return 'wait_for_browser' if waiting else 'recognize_form'

        def recognize(s):
            data['recognition'] = normalize_observation(authority['observation']['evidence']['snapshot'])
            return node('recognize_form')(s)

        def retrieve(s):
            # Exact confirmed facts first. Retrieval is only for unresolved,
            # clearly read questions, bounded to eight requests per advance.
            initial = build_answer_plan(data['recognition'], authority['facts'], context)
            data['candidates'], data['retrieval'] = [], []
            for question in initial.get('questions', [])[:8]:
                try:
                    subject = question['subject_id']
                    response = self.retriever.search(state['user_id'], question['label'],
                        resume_revision_id=state['resume_revision_id'], site_scope=context['site_scope'],
                        entity_ids=[subject] if subject != 'person' else None, limit=3)
                    evidence = response.get('evidence', [])[:3]
                    data['candidates'].extend({**item, 'user_id': state['user_id'],
                        'field_id': question['field_id']} for item in evidence)
                    data['retrieval'].append({'field_id': question['field_id'], 'mode': response['mode'],
                        'candidate_count': len(evidence), 'chunk_refs': [item['chunk_id'] for item in evidence]})
                except Exception as exc:
                    # Local index unavailable is not evidence for an answer.
                    # Exception text may contain personal query/credentials.
                    data['retrieval'].append({'field_id': question['field_id'],
                        'mode': 'unavailable', 'error_type': type(exc).__name__})
            return node('retrieve_evidence')(s)

        def plan(s):
            data['plan'] = build_answer_plan(data['recognition'], authority['facts'], context,
                                             candidates=data['candidates'])
            return node('plan_answers')(s)

        def audit(s):
            recognition, planned = data['recognition'], data['plan']
            # A read-only suggestion is never an executable action or proof of
            # a completed application. No exception is made for high confidence.
            if planned.get('executable_actions'):
                raise ValueError('Read-only graph cannot dispatch browser actions')
            has_questions = bool(planned.get('questions'))
            reason = ('pending_confirmation' if has_questions else
                      'supplementary_observation_required' if recognition['needs_observation'] or planned.get('blocked') else
                      'analysis_complete_read_only')
            return node('audit_coverage', {'status': 'awaiting_user' if has_questions else 'awaiting_browser',
                'reason': reason, 'next_node': 'bind_context'})(s)

        graph = StateGraph(WorkflowState)
        graph.add_node('bind_context', lambda s: {'trace': ['bind_context'], 'resume_from': 'bind_context'})
        graph.add_node('validate_context', node('validate_context'))
        graph.add_node('recognize_form', recognize)
        graph.add_node('retrieve_evidence', retrieve)
        graph.add_node('plan_answers', plan)
        graph.add_node('audit_coverage', audit)
        graph.add_node('fail_closed', node('fail_closed', {'status': 'failed', 'reason': state.get('block_reason'), 'next_node': 'bind_context'}))
        graph.add_node('reconcile', node('reconcile', {'status': 'reconciling', 'reason': 'pending_action_reconciliation', 'next_node': 'bind_context'}))
        graph.add_node('wait_for_user', node('wait_for_user', {'status': 'awaiting_user', 'reason': 'pending_confirmation', 'next_node': 'bind_context'}))
        graph.add_node('wait_for_browser', node('wait_for_browser', {'status': 'awaiting_browser', 'reason': waiting, 'next_node': 'bind_context'}))
        graph.add_edge(START, 'bind_context')
        graph.add_edge('bind_context', 'validate_context')
        graph.add_conditional_edges('validate_context', route,
            {key: key for key in ('fail_closed','reconcile','wait_for_user','wait_for_browser','recognize_form')})
        for a, b in (('recognize_form','retrieve_evidence'),('retrieve_evidence','plan_answers'),('plan_answers','audit_coverage')):
            graph.add_edge(a, b)
        for end in ('fail_closed','reconcile','wait_for_user','wait_for_browser','audit_coverage'):
            graph.add_edge(end, END)
        outcome = graph.compile().invoke(state, config={'recursion_limit': 10, 'callbacks': []})
        return outcome, data if 'plan' in data else None

    def result_metadata(self, artifacts):
        if not artifacts:
            return {'workflow_stage': 'd2_read_only', 'analysis_available': False}
        return {'workflow_stage': 'd2_read_only', 'analysis_available': True,
                'question_count': len(artifacts['recognition']['fields']),
                'suggestion_count': len(artifacts['plan']['suggestions']),
                'confirmation_count': len(artifacts['plan']['questions']),
                'blocked_count': len(artifacts['plan']['blocked'])}

    def validate_artifacts(self, authority, outcome, artifacts):
        if artifacts and self._observation_issue(outcome, authority):
            raise StaleWork('Observation evidence expired or changed during analysis')

    def commit_artifacts(self, conn, *, user_id, run_id, checkpoint_id, outcome, artifacts):
        if not artifacts:
            return
        observation_id = outcome['observation_id']
        ids = {}
        for field in artifacts['recognition']['fields']:
            if not field.get('question'):
                continue
            identity = str(uuid5(NAMESPACE_URL, encode([user_id, observation_id, field['id']])))
            ids[field['id']] = identity
            conn.execute(text("""INSERT INTO zhida.questions
                (id,user_id,observation_id,label,section,entity_binding,control_type,options,evidence_refs)
                VALUES(:id,:u,:o,:label,:section,:entity,:kind,CAST(:options AS jsonb),CAST(:refs AS jsonb))
                ON CONFLICT(id) DO NOTHING"""),
                {'id': identity, 'u': user_id, 'o': observation_id, 'label': field['question'],
                 'section': ' / '.join(field.get('section_path', [])),
                 'entity': encode(field.get('entity_binding', {})), 'kind': field['control_kind'],
                 'options': encode(field['options']), 'refs': encode({
                     'schema_version': 'd2-question-evidence-v1', 'refs': field['evidence_refs'],
                     'date_precision': field.get('date_precision'), 'multiple': field.get('multiple', False)})})
        for question in artifacts['plan']['questions']:
            identity = str(uuid5(NAMESPACE_URL, encode([checkpoint_id, question['field_id'], 'confirmation'])))
            conn.execute(text("""INSERT INTO zhida.confirmation_requests
                (id,user_id,run_id,question_id,question_revision,label,semantic_key,subject_id,scope_key,options,reason)
                VALUES(:id,:u,:r,:q,:rev,:label,:key,:subject,:scope,CAST(:options AS jsonb),:reason)"""),
                {'id': identity, 'u': user_id, 'r': run_id, 'q': ids[question['field_id']],
                 'rev': outcome['output_revision'], 'label': question['label'], 'key': question['semantic_key'],
                 'subject': question['subject_id'], 'scope': question['scope_key'],
                 'options': encode(question['options']), 'reason': question['reason']})
        report = {'schema_version': 'd2-analysis-v1', 'recognition': artifacts['recognition'],
                  'plan': artifacts['plan'], 'retrieval': artifacts['retrieval'],
                  'read_only': True, 'browser_actions_executed': 0, 'model_calls_executed': 0,
                  'application_complete': False}
        conn.execute(text("""INSERT INTO zhida.form_analysis_reports
            (id,user_id,run_id,observation_id,checkpoint_id,run_revision,report)
            VALUES(:id,:u,:r,:o,:c,:rev,CAST(:report AS jsonb))"""),
            {'id': str(uuid5(NAMESPACE_URL, checkpoint_id + ':report')), 'u': user_id, 'r': run_id,
             'o': observation_id, 'c': checkpoint_id, 'rev': outcome['output_revision'], 'report': encode(report)})
