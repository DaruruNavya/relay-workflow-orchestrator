"""Durable job queue on SQLite (table queue_jobs): jobs survive restarts and a crashed worker's lease expires.

States: queued -> leased (worker_id, leased_until) -> deleted when the run finishes or pauses.
A leased job whose lease has expired is claimable again; that is the crash-recovery mechanism.
"""
from __future__ import annotations

import time

from .db import Store


def enqueue(conn, run_id: str) -> None:
    """Called inside the caller's transaction so 'run created' and 'job queued' commit together."""
    conn.execute(
        "INSERT OR REPLACE INTO queue_jobs(run_id, status, worker_id, leased_until, deliveries, enqueued_at) "
        "VALUES (?, 'queued', NULL, NULL, 0, ?)", (run_id, time.time()))


def remove(conn, run_id: str) -> None:
    conn.execute("DELETE FROM queue_jobs WHERE run_id=?", (run_id,))


class JobQueue:
    def __init__(self, store: Store, lease_seconds: float):
        self.store, self.lease = store, lease_seconds

    def claim(self, worker_id: str) -> dict | None:
        now = time.time()
        with self.store.tx() as c:
            row = c.execute(
                "SELECT * FROM queue_jobs WHERE status='queued' OR (status='leased' AND leased_until < ?) "
                "ORDER BY enqueued_at LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE queue_jobs SET status='leased', worker_id=?, leased_until=?, deliveries=deliveries+1 "
                      "WHERE run_id=?", (worker_id, now + self.lease, row["run_id"]))
            job = dict(row)
            job["deliveries"] += 1
            return job

    def renew(self, run_id: str, worker_id: str) -> bool:
        with self.store.tx() as c:
            cur = c.execute("UPDATE queue_jobs SET leased_until=? WHERE run_id=? AND worker_id=? AND status='leased'",
                            (time.time() + self.lease, run_id, worker_id))
            return cur.rowcount == 1

    def release(self, run_id: str, worker_id: str) -> None:
        """Worker is done with the job (idempotent; never touches a job re-enqueued by an approval)."""
        with self.store.tx() as c:
            c.execute("DELETE FROM queue_jobs WHERE run_id=? AND worker_id=? AND status='leased'", (run_id, worker_id))
