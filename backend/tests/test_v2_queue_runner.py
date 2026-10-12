from dataclasses import replace

import pytest

from app.backend_v2.runner_protocol import (BrowserCommand, RunnerJournal, StaleCommand,
                                           UnknownOutcome)


def runner(path):
    journal = RunnerJournal(path, user_id='synthetic-user', device_id='synthetic-device')
    journal.bind(resource_id='safari', epoch=1, connection_epoch=1, document_id='document-a', observation_revision=1)
    return journal


def command(**changes):
    return replace(BrowserCommand('action-1', 'safari', 1, 1, 'document-a', 1, 'set_text', 'fact-reference'),
                   **changes)


def test_verified_command_is_not_replayed_after_runner_restart(tmp_path):
    path = tmp_path / 'runner.sqlite3'
    journal, calls = runner(path), []
    cmd = command()
    result = journal.execute(cmd, perform=calls.append, verify=lambda _: (True, 'observation-after'))
    assert result.status == 'verified' and calls == [cmd]
    restarted = runner(path)
    again = restarted.execute(cmd, perform=lambda _: pytest.fail('duplicate website write'),
                              verify=lambda _: pytest.fail('duplicate verification'))
    assert again.reused and again.status == 'verified'


def test_unknown_outcome_blocks_new_epoch_until_readonly_reconciliation(tmp_path):
    path = tmp_path / 'runner.sqlite3'
    journal = runner(path)
    def disconnected(_):
        raise ConnectionError('lost after sending')
    with pytest.raises(ConnectionError):
        journal.execute(command(), perform=disconnected, verify=lambda _: (True, 'unused'))
    restarted = RunnerJournal(path, user_id='synthetic-user', device_id='synthetic-device')
    restarted.bind(resource_id='safari', epoch=2, connection_epoch=2, document_id='document-a', observation_revision=2)
    cmd = command(action_id='action-2', epoch=2, connection_epoch=2, observation_revision=2)
    with pytest.raises(UnknownOutcome):
        restarted.execute(cmd, perform=lambda _: pytest.fail('new epoch must not race unknown action'),
                          verify=lambda _: (True, 'unused'))
    receipt = restarted.reconcile('action-1', epoch=2, connection_epoch=2, verified=True,
                                  evidence_ref='new-readonly-observation')
    assert receipt.status == 'verified'
    assert restarted.execute(cmd, perform=lambda _: None,
                             verify=lambda _: (True, 'new-observation')).status == 'verified'


def test_wrong_window_stale_lease_and_reused_id_are_rejected(tmp_path):
    journal = runner(tmp_path / 'runner.sqlite3')
    with pytest.raises(StaleCommand):
        journal.execute(command(document_id='different'), perform=lambda _: pytest.fail('wrong window'),
                        verify=lambda _: (True, 'unused'))
    journal.execute(command(), perform=lambda _: None, verify=lambda _: (True, 'verified'))
    with pytest.raises(ValueError):
        journal.execute(command(payload_ref='other-fact'), perform=lambda _: pytest.fail('reused ID'),
                        verify=lambda _: (True, 'unused'))
    journal.bind(resource_id='safari', epoch=2, connection_epoch=2, document_id='document-b', observation_revision=2)
    with pytest.raises(StaleCommand):
        journal.execute(command(action_id='old-worker-command'), perform=lambda _: pytest.fail('stale worker'),
                        verify=lambda _: (True, 'unused'))


def test_journal_cannot_be_reused_by_another_user_or_submit(tmp_path):
    path = tmp_path / 'runner.sqlite3'
    journal = runner(path)
    with pytest.raises(ValueError):
        RunnerJournal(path, user_id='another', device_id='synthetic-device')
    with pytest.raises(ValueError):
        journal.execute(command(command_type='final_submit'), perform=lambda _: pytest.fail('submit'),
                        verify=lambda _: (True, 'unused'))


def test_connection_changed_during_execution_keeps_resource_quarantined(tmp_path):
    journal = runner(tmp_path / 'runner.sqlite3')
    def changed(_):
        journal.bind(resource_id='safari', epoch=2, connection_epoch=2,
                     document_id='document-a', observation_revision=2)
    receipt = journal.execute(command(), perform=changed, verify=lambda _: (True, 'old-connection-observation'))
    assert receipt.status == 'unknown'
    with pytest.raises(UnknownOutcome):
        journal.execute(command(action_id='new', epoch=2, connection_epoch=2, observation_revision=2),
                        perform=lambda _: pytest.fail('must reconcile first'), verify=lambda _: (True, 'unused'))


def test_same_connection_cannot_restore_an_old_observation(tmp_path):
    journal = runner(tmp_path / 'runner.sqlite3')
    journal.bind(resource_id='safari', epoch=1, connection_epoch=1,
                 document_id='document-a', observation_revision=2)
    with pytest.raises(StaleCommand):
        journal.bind(resource_id='safari', epoch=1, connection_epoch=1,
                     document_id='document-a', observation_revision=1)
    with pytest.raises(StaleCommand):
        journal.bind(resource_id='safari', epoch=1, connection_epoch=1,
                     document_id='document-b', observation_revision=2)
