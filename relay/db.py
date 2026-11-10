"""SQLite persistence. One connection per operation (thread-safe); WAL so API and workers share the file.

The database is also the durable queue (queue_jobs). Every state transition that matters runs inside a
single BEGIN IMMEDIATE transaction, so a killed process can never leave a half-written step behind.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
    status TEXT NOT NULL, definition TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
    definition_snapshot TEXT NOT NULL,
    status TEXT NOT NULL, trigger_type TEXT NOT NULL, input TEXT NOT NULL,
    current_node_id TEXT, steps_executed INTEGER NOT NULL DEFAULT 0,
    ai_tokens_used INTEGER NOT NULL DEFAULT 0,
    error TEXT, ended_by TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
    wake_at REAL, started_at TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_workflow ON runs(workflow_id, started_at);
CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
    node_id TEXT NOT NULL, node_type TEXT NOT NULL, status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    resolved_input TEXT, output TEXT,
    tokens_prompt INTEGER, tokens_completion INTEGER,
    idempotency_key TEXT, approval_id TEXT, error TEXT,
    started_at TEXT, duration_ms INTEGER,
    PRIMARY KEY (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
    message TEXT, status TEXT NOT NULL,
    decided_by TEXT, decided_at TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, status);
CREATE TABLE IF NOT EXISTS queue_jobs (
    run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    worker_id TEXT, leased_until REAL, deliveries INTEGER NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        with self.connection() as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self):
        """Write transaction: all-or-nothing, serialized across processes."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def all(self, sql: str, params=()) -> list[dict]:
        with self.connection() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None
