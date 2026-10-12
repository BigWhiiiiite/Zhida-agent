"""D1: real LangGraph orchestration with atomic business-boundary persistence.

The graph is bounded and has NO browser/model/fact-writing nodes yet. A waiting
state releases the worker. On a later wakeup a fresh engine reads only the
committed head and revalidates current domain state from ``bind_context``. It
never jumps back to a previously planned side effect.

This is an application-level checkpoint, NOT LangGraph's PostgresSaver and NOT
``interrupt()/Command(resume=...)``. Per-node durable interrupts/time travel are
not advertised. All D1 nodes are pure decisions over a read-only DB snapshot;
the resulting checkpoint, step receipt, run revision and event are persisted by
``WorkResult.effects`` inside QueueStore.complete's existing Inbox transaction.
Crashes before that transaction leave nothing to resume; after commit, the
Inbox/revision/unique step key prevent a duplicate committed advance.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TypedDict
from uuid import NAMESPACE_URL, uuid5

from langgraph.graph import END, START, StateGraph
from langsmith.run_helpers import tracing_context
from sqlalchemy import text

from .messaging import LeaseLost
from .repository import digest, emit_event, encode
from .workers import StaleWork, WorkResult

WORKFLOW_VERSION = 'langgraph-d1-v1'
STATE_VERSION = 1
_TERMINAL = frozenset({'cancelled', 'ready_for_review', 'failed'})
_NODES_PER_ADVANCE = 3


class WorkflowState(TypedDict, total=False):
    schema_version: int
    user_id: str
    run_id: str
    workflow_version: str
    input_revision: int
    output_revision: int
    resume_revision_id: str
    browser_session_id: str | None
    browser_connection_epoch: int | None
    observation_id: str | None
    pending_confirmation_count: int
    unresolved_action_count: int
    step_no: int
    total_node_steps: int
    previous_checkpoint_id: str | None
    resume_from: str
    next_node: str
    status: str
    reason: str
    block_reason: str | None
    trace: list[str]


def _context_value(context, key: str, default=None):
    return context.get(key, default) if isinstance(context, Mapping) else getattr(context, key, default)


def _check_lease(context) -> None:
    lost = _context_value(context, 'lease_lost')
    if lost and lost():
        raise LeaseLost('Workflow execution lease was lost')


def _budget_reason(budget: Any, *, step_no: int, total_node_steps: int, now: datetime) -> str | None:
    if not isinstance(budget, dict):
        return 'invalid_workflow_budget'
    limits = (('max_advances', 64, 256, step_no),
              ('max_graph_steps', 512, 4096, total_node_steps))
    for key, default, ceiling, used in limits:
        limit = budget.get(key, default)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= ceiling:
            return 'invalid_workflow_budget'
        if used > limit:
            return 'workflow_budget_exhausted'
    deadline = budget.get('deadline_at')
    if deadline is not None:
        try:
            parsed = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return 'invalid_workflow_budget'
        except (AttributeError, ValueError, TypeError):
            return 'invalid_workflow_budget'
        if now >= parsed:
            return 'workflow_deadline_exceeded'
    return None


def build_graph():
    """Compile the side-effect-free D1 decision graph; usable in pure tests."""
    def bind_context(state: WorkflowState):
        # The engine supplies freshly read authoritative reference data, not a
        # cached resume body or facts from an old checkpoint.
        return {'resume_from': 'bind_context', 'trace': ['bind_context']}

    def validate_context(state: WorkflowState):
        return {'trace': [*state['trace'], 'validate_context']}

    def route(state: WorkflowState):
        if state.get('block_reason'):
            return 'fail_closed'
        if state['unresolved_action_count']:
            return 'reconcile'
        if state['pending_confirmation_count']:
            return 'wait_for_user'
        return 'wait_for_browser'

    def fail_closed(state: WorkflowState):
        return {'status': 'failed', 'reason': state['block_reason'],
                'next_node': 'bind_context', 'trace': [*state['trace'], 'fail_closed']}

    def reconcile(state: WorkflowState):
        return {'status': 'reconciling', 'reason': 'pending_action_reconciliation',
                'next_node': 'bind_context', 'trace': [*state['trace'], 'reconcile']}

    def wait_for_user(state: WorkflowState):
        return {'status': 'awaiting_user', 'reason': 'pending_confirmation',
                'next_node': 'bind_context', 'trace': [*state['trace'], 'wait_for_user']}

    def wait_for_browser(state: WorkflowState):
        reason = 'browser_not_bound' if not state.get('browser_session_id') else 'recognition_not_enabled'
        return {'status': 'awaiting_browser', 'reason': reason,
                'next_node': 'bind_context', 'trace': [*state['trace'], 'wait_for_browser']}

    builder = StateGraph(WorkflowState)
    for name, node in (('bind_context', bind_context), ('validate_context', validate_context),
                       ('fail_closed', fail_closed), ('reconcile', reconcile),
                       ('wait_for_user', wait_for_user), ('wait_for_browser', wait_for_browser)):
        builder.add_node(name, node)
    builder.add_edge(START, 'bind_context')
    builder.add_edge('bind_context', 'validate_context')
    builder.add_conditional_edges('validate_context', route,
                                 {key: key for key in ('fail_closed', 'reconcile', 'wait_for_user', 'wait_for_browser')})
    for name in ('fail_closed', 'reconcile', 'wait_for_user', 'wait_for_browser'):
        builder.add_edge(name, END)
    return builder.compile()


def _read_authority(conn, user_id: str, run_id: str, *, lock: bool = False) -> dict:
    run = conn.execute(text("""SELECT id,run_revision,status,workflow_version,resume_revision_id,
        browser_session_id,budget FROM zhida.application_runs
        WHERE id=:r AND user_id=:u""" + (' FOR UPDATE' if lock else '')),
        {'r': run_id, 'u': user_id}).mappings().first()
    if run is None:
        raise StaleWork('Run does not exist in this tenant')
    run = dict(run)
    selected = conn.execute(text("""SELECT v.id FROM zhida.resume_revisions v
        JOIN zhida.resumes r ON r.id=v.resume_id AND r.user_id=v.user_id
        WHERE v.id=:id AND v.user_id=:u AND r.deleted_at IS NULL"""),
        {'id': run['resume_revision_id'], 'u': user_id}).scalar()
    epoch = None
    if run['browser_session_id']:
        epoch = conn.execute(text("""SELECT connection_epoch FROM zhida.browser_sessions
            WHERE id=:id AND user_id=:u"""), {'id': run['browser_session_id'], 'u': user_id}).scalar()
    pending = conn.execute(text("""SELECT id,question_revision FROM zhida.confirmation_requests
        WHERE run_id=:r AND user_id=:u AND status='pending' ORDER BY id"""),
        {'r': run_id, 'u': user_id}).all()
    unresolved = conn.execute(text("""SELECT action_id,status FROM zhida.action_ledger
        WHERE run_id=:r AND user_id=:u AND status IN ('dispatched','unknown') ORDER BY action_id"""),
        {'r': run_id, 'u': user_id}).all()
    observation = conn.execute(text("""SELECT id FROM zhida.form_observations
        WHERE run_id=:r AND user_id=:u ORDER BY observation_revision DESC LIMIT 1"""),
        {'r': run_id, 'u': user_id}).scalar()
    # Only references/counts are passed to the graph. A fingerprint catches
    # domain updates which do not (yet) increment application_runs.run_revision.
    fingerprint = digest([run, bool(selected), epoch, [list(row) for row in pending],
                          [list(row) for row in unresolved], observation])
    return {'run': run, 'resume_exists': bool(selected), 'browser_epoch': epoch,
            'pending_count': len(pending), 'unresolved_count': len(unresolved),
            'observation_id': observation, 'fingerprint': fingerprint}


def _read_head(conn, user_id: str, run_id: str, version: str = WORKFLOW_VERSION) -> dict | None:
    row = conn.execute(text("""SELECT c.id,c.state,c.step_no,c.output_revision FROM zhida.workflow_heads h
        JOIN zhida.workflow_checkpoints c ON c.id=h.checkpoint_id AND c.user_id=h.user_id
          AND c.run_id=h.run_id AND c.workflow_version=h.workflow_version
        WHERE h.user_id=:u AND h.run_id=:r AND h.workflow_version=:v"""),
        {'u': user_id, 'r': run_id, 'v': version}).mappings().first()
    return dict(row) if row else None


def _assert_current(authority: dict, expected_revision: int, version: str = WORKFLOW_VERSION) -> None:
    run = authority['run']
    if run['run_revision'] != expected_revision or run['status'] in _TERMINAL:
        raise StaleWork('Run revision or terminal state changed')
    if run['workflow_version'] != version:
        raise StaleWork('Workflow version must be explicitly pinned')


class DurableWorkflowEngine:
    workflow_version = WORKFLOW_VERSION
    accepts_execution_context = True
    handles_reconciliation = True
    nodes_per_advance = _NODES_PER_ADVANCE

    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.graph = build_graph()

    def read_authority(self, conn, user_id, run_id, *, lock=False):
        return _read_authority(conn, user_id, run_id, lock=lock)

    def evaluate(self, state, authority):
        return self.graph.invoke(state, config={'recursion_limit': 8, 'callbacks': []}), None

    def commit_artifacts(self, conn, *, user_id, run_id, checkpoint_id, outcome, artifacts):
        """Extension seam for atomically committed, non-executable D2 reports."""

    def result_metadata(self, artifacts):
        return {}

    def validate_artifacts(self, authority, outcome, artifacts):
        """Validate time-sensitive evidence immediately before commit."""

    def advance(self, *, user_id: str, run_id: str, expected_revision: int,
                execution_context=None) -> WorkResult:
        _check_lease(execution_context)
        with self.db.transaction(user_id) as conn:
            authority = self.read_authority(conn, user_id, run_id)
            _assert_current(authority, expected_revision, self.workflow_version)
            head = _read_head(conn, user_id, run_id, self.workflow_version)
        if head:
            saved = head['state']
            expected_binding = {'schema_version': STATE_VERSION, 'user_id': user_id,
                                'run_id': run_id, 'workflow_version': self.workflow_version,
                                'output_revision': head['output_revision'], 'step_no': head['step_no']}
            if (head['output_revision'] > expected_revision or not isinstance(saved, dict) or
                    any(saved.get(key) != value for key, value in expected_binding.items())):
                raise StaleWork('Committed workflow head is incompatible with current run')
        step_no = (head['step_no'] if head else 0) + 1
        previous_steps = head['state'].get('total_node_steps', 0) if head else 0
        if isinstance(previous_steps, bool) or not isinstance(previous_steps, int) or previous_steps < 0:
            raise StaleWork('Committed workflow budget is invalid')
        run = authority['run']
        block_reason = _budget_reason(run['budget'], step_no=step_no,
                                     total_node_steps=previous_steps + self.nodes_per_advance, now=self.clock())
        if not authority['resume_exists']:
            block_reason = 'resume_unavailable'
        elif run['browser_session_id'] and authority['browser_epoch'] is None:
            block_reason = 'browser_binding_unavailable'
        state: WorkflowState = {
            'schema_version': STATE_VERSION, 'user_id': user_id, 'run_id': run_id,
            'workflow_version': self.workflow_version, 'input_revision': expected_revision,
            'output_revision': expected_revision + 1, 'resume_revision_id': run['resume_revision_id'],
            'browser_session_id': run['browser_session_id'], 'browser_connection_epoch': authority['browser_epoch'],
            'observation_id': authority['observation_id'], 'pending_confirmation_count': authority['pending_count'],
            'unresolved_action_count': authority['unresolved_count'], 'step_no': step_no,
            'total_node_steps': previous_steps + self.nodes_per_advance,
            'previous_checkpoint_id': head['id'] if head else None, 'block_reason': block_reason,
        }
        # Ambient tracing credentials must not turn local orchestration metadata
        # into an implicit network disclosure to a third-party tracing service.
        with tracing_context(enabled=False):
            outcome, artifacts = self.evaluate(state, authority)
        _check_lease(execution_context)
        step_id = str(uuid5(NAMESPACE_URL, encode([user_id, run_id, self.workflow_version, expected_revision])))
        checkpoint_id = str(uuid5(NAMESPACE_URL, step_id + ':checkpoint'))
        result = {'workflow_version': self.workflow_version, 'langgraph_enabled': True,
                  'reason': outcome['reason'], 'checkpoint_id': checkpoint_id, 'step_no': step_no,
                  'run_revision': expected_revision + 1, 'next_node': outcome['next_node'],
                  'browser_actions_executed': 0, 'model_calls_executed': 0,
                  'resume_mode': 'revalidate_committed_boundary', **self.result_metadata(artifacts)}

        def effects(conn):
            _check_lease(execution_context)
            # Match WorkerDispatcher's lock order: execution lease, then run.
            epoch = _context_value(execution_context, 'lease_epoch')
            if epoch is not None:
                live = conn.execute(text("""SELECT epoch FROM zhida.run_leases
                    WHERE resource_id=:r AND user_id=:u AND epoch=:epoch AND expires_at>now()
                    FOR UPDATE"""), {'r': run_id, 'u': user_id, 'epoch': epoch}).first()
                if not live:
                    raise LeaseLost('Workflow lease fencing check failed')
            current = self.read_authority(conn, user_id, run_id, lock=True)
            _assert_current(current, expected_revision, self.workflow_version)
            if current['fingerprint'] != authority['fingerprint']:
                raise StaleWork('Authoritative context changed during graph execution')
            self.validate_artifacts(current, outcome, artifacts)
            current_head = _read_head(conn, user_id, run_id, self.workflow_version)
            if (current_head['id'] if current_head else None) != (head['id'] if head else None):
                raise StaleWork('Committed workflow head changed')
            params = {'id': checkpoint_id, 'u': user_id, 'r': run_id, 'v': self.workflow_version,
                      'step': step_no, 'input': expected_revision, 'output': expected_revision + 1,
                      'state': encode(outcome), 'step_id': step_id, 'epoch': epoch,
                      'event': _context_value(execution_context, 'event_id'),
                      'job': _context_value(execution_context, 'job_id'),
                      'status': outcome['status'], 'reason': outcome['reason']}
            conn.execute(text("""INSERT INTO zhida.workflow_checkpoints
                (id,user_id,run_id,workflow_version,step_no,input_revision,output_revision,state)
                VALUES(:id,:u,:r,:v,:step,:input,:output,CAST(:state AS jsonb))"""), params)
            conn.execute(text("""INSERT INTO zhida.workflow_step_commits
                (id,user_id,run_id,workflow_version,input_revision,output_revision,step_no,
                 checkpoint_id,lease_epoch,event_id,job_id,status,reason)
                VALUES(:step_id,:u,:r,:v,:input,:output,:step,:id,:epoch,:event,:job,:status,:reason)"""), params)
            conn.execute(text("""INSERT INTO zhida.workflow_heads
                (user_id,run_id,workflow_version,checkpoint_id,step_no,output_revision)
                VALUES(:u,:r,:v,:id,:step,:output)
                ON CONFLICT(user_id,run_id,workflow_version) DO UPDATE SET
                    checkpoint_id=EXCLUDED.checkpoint_id,step_no=EXCLUDED.step_no,
                    output_revision=EXCLUDED.output_revision,updated_at=now()"""), params)
            conn.execute(text("""UPDATE zhida.application_runs SET status=:status,run_revision=:output,updated_at=now()
                WHERE user_id=:u AND id=:r AND run_revision=:input"""), params)
            self.commit_artifacts(conn, user_id=user_id, run_id=run_id,
                                  checkpoint_id=checkpoint_id, outcome=outcome, artifacts=artifacts)
            emit_event(conn, user_id, run_id, 'workflow.waiting' if outcome['status'] != 'failed' else 'workflow.failed', result)

        return WorkResult(outcome['status'], result, effects)
