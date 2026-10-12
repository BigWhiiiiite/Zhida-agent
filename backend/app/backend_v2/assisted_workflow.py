"""D3: Worker authorizes a bounded local round; never owns a desktop browser.

The local round has its own recognition/planning graph and typed driver. A wake
commits a checkpoint and Runner authorization atomically with Inbox completion.
No worker waits for a user, a desktop connection, or a slow external model.
"""
from langgraph.graph import START, END, StateGraph
from sqlalchemy import text

from .repository import digest
from .workflow import DurableWorkflowEngine, WorkflowState

D3_VERSION = 'langgraph-d3-v1'


class AssistedWorkflowEngine(DurableWorkflowEngine):
    workflow_version = D3_VERSION

    def read_authority(self, conn, user_id, run_id, *, lock=False):
        data = super().read_authority(conn, user_id, run_id, lock=lock)
        row = conn.execute(text('SELECT * FROM zhida.assisted_rounds WHERE user_id=:u AND run_id=:r '
                                'ORDER BY created_at DESC LIMIT 1'), {'u':user_id,'r':run_id}).mappings().first()
        data['round'] = dict(row) if row else None
        data['local_unresolved'] = conn.execute(text("""SELECT count(*) FROM zhida.assisted_actions a
            JOIN zhida.assisted_rounds s ON s.id=a.round_id AND s.user_id=a.user_id
            WHERE a.user_id=:u AND s.run_id=:r AND a.status IN ('dispatched','unknown')"""),
            {'u':user_id,'r':run_id}).scalar_one()
        # Progress can change while polling; only authoritative decision inputs
        # take part in the commit CAS fingerprint.
        decision = {k: data['round'][k] for k in ('id','status','cancel_requested','result')} if row else None
        data['fingerprint'] = digest([data['fingerprint'],decision,data['local_unresolved']])
        return data

    def evaluate(self, state, authority):
        item = authority['round']
        def route(s):
            if s.get('block_reason') or not item: return 'fail_closed'
            if authority['local_unresolved'] or s['unresolved_action_count']: return 'reconcile'
            if item['cancel_requested'] or item['status']=='interrupted': return 'wait_for_browser'
            if item['status']=='queued': return 'dispatch_local_round'
            if item['status']=='finished':
                return 'audit_coverage'
            return 'wait_for_browser'

        def node(name, status, reason):
            return lambda s: {'trace':[*s.get('trace',[]),name], 'status':status,
                              'reason':reason,'next_node':'bind_context'}
        def audit(s):
            result = item.get('result') or {}
            status = result.get('status')
            check=result.get('pre_submit') or {}
            ready = status=='ready_for_review' and check.get('ready') is True
            return node('audit_coverage', 'ready_for_review' if ready else
                'awaiting_user' if status=='needs_user' else 'awaiting_browser',
                'human_final_review' if ready else 'confirmation_required' if status=='needs_user'
                else 'local_round_incomplete')(s)

        graph = StateGraph(WorkflowState)
        graph.add_node('bind_context',lambda s:{'trace':['bind_context'],'resume_from':'bind_context'})
        graph.add_node('validate_context',lambda s:{'trace':[*s['trace'],'validate_context']})
        graph.add_node('dispatch_local_round',node('dispatch_local_round','awaiting_browser','local_round_authorized'))
        graph.add_node('reconcile',node('reconcile','reconciling','local_action_outcome_unknown'))
        graph.add_node('wait_for_browser',node('wait_for_browser','awaiting_browser','local_runner_or_user_required'))
        graph.add_node('fail_closed',node('fail_closed','failed',state.get('block_reason') or 'local_round_missing'))
        graph.add_node('audit_coverage',audit)
        graph.add_edge(START,'bind_context'); graph.add_edge('bind_context','validate_context')
        graph.add_conditional_edges('validate_context',route,{n:n for n in
            ('dispatch_local_round','reconcile','wait_for_browser','fail_closed','audit_coverage')})
        for name in ('dispatch_local_round','reconcile','wait_for_browser','fail_closed','audit_coverage'):
            graph.add_edge(name,END)
        return graph.compile().invoke(state,config={'recursion_limit':8,'callbacks':[]}), {'round_id':item['id'] if item else None}

    def commit_artifacts(self, conn, *, user_id, run_id, checkpoint_id, outcome, artifacts):
        if outcome['reason']=='local_round_authorized':
            changed = conn.execute(text("UPDATE zhida.assisted_rounds SET status='authorized',updated_at=now() "
                "WHERE id=:id AND user_id=:u AND status='queued' AND NOT cancel_requested"),
                {'id':artifacts['round_id'],'u':user_id}).rowcount
            if changed!=1:
                from .workers import StaleWork
                raise StaleWork('Runner authorization changed')

    def result_metadata(self, artifacts):
        return {'workflow_stage':'d3_local_runner','local_round_id':(artifacts or {}).get('round_id'),
                'browser_actions_executed':0,'model_calls_executed':0}
