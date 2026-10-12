"""Durable local command journal and fake-transport boundary, not a browser driver.

The journal is an explicitly supplied new local SQLite file containing opaque IDs,
command hashes and receipt references only. It never opens a recruiting website.
Server authentication, authorization and live page checks belong to the gateway
and a future transport; a caller cannot obtain those permissions from this log.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

COMMAND_TYPES = frozenset({'observe', 'set_text', 'select_option', 'set_date', 'check_option',
                           'upload_file', 'add_record', 'save_draft'})


class StaleCommand(RuntimeError):
    pass


class UnknownOutcome(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserCommand:
    action_id: str
    resource_id: str
    epoch: int
    connection_epoch: int
    document_id: str
    observation_revision: int
    command_type: str
    payload_ref: str

    def digest(self) -> str:
        if self.command_type not in COMMAND_TYPES or not self.action_id or not self.payload_ref:
            raise ValueError('Unsupported command or missing reference')
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class CommandReceipt:
    action_id: str
    status: str
    evidence_ref: str = ''
    reused: bool = False


class RunnerJournal:
    def __init__(self, path: Path, *, user_id: str, device_id: str):
        if not user_id or not device_id:
            raise ValueError('Runner journal requires a user and device')
        self.path, self.user_id, self.device_id = str(path), user_id, device_id
        with self._connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS journal_owner (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), user_id TEXT NOT NULL, device_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS resources (
                    id TEXT PRIMARY KEY, epoch INTEGER NOT NULL, connection_epoch INTEGER NOT NULL,
                    document_id TEXT NOT NULL, observation_revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, resource_id TEXT NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL, evidence_ref TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(resource_id) REFERENCES resources(id));
            ''')
            conn.execute('INSERT OR IGNORE INTO journal_owner VALUES(1,?,?)', (user_id, device_id))
            owner = conn.execute('SELECT user_id,device_id FROM journal_owner WHERE singleton=1').fetchone()
            if owner != (user_id, device_id):
                raise ValueError('Journal belongs to a different user or device')

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def bind(self, *, resource_id: str, epoch: int, connection_epoch: int,
             document_id: str, observation_revision: int):
        if min(epoch, connection_epoch, observation_revision) < 1 or not document_id:
            raise ValueError('Invalid resource binding')
        with self._transaction() as conn:
            previous = conn.execute('SELECT epoch,connection_epoch,document_id,observation_revision FROM resources WHERE id=?',
                                    (resource_id,)).fetchone()
            if previous and (epoch < previous[0] or connection_epoch < previous[1]):
                raise StaleCommand('Binding is from an older lease or connection')
            if previous and connection_epoch == previous[1] and (
                    observation_revision < previous[3] or
                    (document_id != previous[2] and observation_revision <= previous[3])):
                raise StaleCommand('Observation revision cannot go backwards or reuse a different document')
            conn.execute('''INSERT INTO resources VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                epoch=excluded.epoch,connection_epoch=excluded.connection_epoch,
                document_id=excluded.document_id,observation_revision=excluded.observation_revision''',
                         (resource_id, epoch, connection_epoch, document_id, observation_revision))

    def _prepare(self, command: BrowserCommand):
        digest = command.digest()
        with self._transaction() as conn:
            existing = conn.execute('SELECT digest,state,evidence_ref FROM actions WHERE id=?',
                                    (command.action_id,)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ValueError('Action ID cannot be reused with different command content')
                if existing[1] in {'dispatched', 'unknown'}:
                    raise UnknownOutcome('Previous execution requires observation and reconciliation')
                return CommandReceipt(command.action_id, existing[1], existing[2], reused=True)
            expected = conn.execute('''SELECT epoch,connection_epoch,document_id,observation_revision
                FROM resources WHERE id=?''', (command.resource_id,)).fetchone()
            if expected != (command.epoch, command.connection_epoch, command.document_id,
                            command.observation_revision):
                raise StaleCommand('Lease, connection or observation is no longer current')
            if conn.execute("SELECT 1 FROM actions WHERE resource_id=? AND state IN ('dispatched','unknown')",
                            (command.resource_id,)).fetchone():
                raise UnknownOutcome('Resource is quarantined until its pending action is reconciled')
            conn.execute("INSERT INTO actions(id,resource_id,digest,state) VALUES(?,?,?,'dispatched')",
                         (command.action_id, command.resource_id, digest))
        return None

    def execute(self, command: BrowserCommand, *, perform: Callable, verify: Callable) -> CommandReceipt:
        receipt = self._prepare(command)
        if receipt:
            return receipt
        try:
            perform(command)
            verified, evidence_ref = verify(command)
            if not evidence_ref:
                raise ValueError('Verification requires an evidence reference')
            status = 'verified' if verified else 'failed'
        except BaseException:
            with self._transaction() as conn:
                conn.execute("UPDATE actions SET state='unknown' WHERE id=?", (command.action_id,))
            raise
        with self._transaction() as conn:
            current = conn.execute('''SELECT epoch,connection_epoch,document_id,observation_revision
                FROM resources WHERE id=?''', (command.resource_id,)).fetchone()
            if current != (command.epoch, command.connection_epoch, command.document_id,
                           command.observation_revision):
                status = 'unknown'
            conn.execute('UPDATE actions SET state=?,evidence_ref=? WHERE id=?',
                         (status, evidence_ref, command.action_id))
        return CommandReceipt(command.action_id, status, evidence_ref)

    def reconcile(self, action_id: str, *, epoch: int, connection_epoch: int,
                  verified: bool, evidence_ref: str) -> CommandReceipt:
        """Caller obtains new read-only evidence; this never invokes perform()."""
        if not evidence_ref:
            raise ValueError('Reconciliation requires new observation evidence')
        with self._transaction() as conn:
            row = conn.execute('''SELECT a.state,r.epoch,r.connection_epoch FROM actions a
                JOIN resources r ON r.id=a.resource_id WHERE a.id=?''', (action_id,)).fetchone()
            if row is None:
                raise LookupError('Unknown action')
            if row[1:] != (epoch, connection_epoch):
                raise StaleCommand('Reconciliation belongs to an old connection')
            if row[0] not in {'dispatched', 'unknown'}:
                raise ValueError('Only unresolved actions can be reconciled')
            status = 'verified' if verified else 'failed'
            conn.execute('UPDATE actions SET state=?,evidence_ref=? WHERE id=?', (status, evidence_ref, action_id))
        return CommandReceipt(action_id, status, evidence_ref)
