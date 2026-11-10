"""The orchestration engine: executes ONE run, step by step, durably.

Run state machine
-----------------
    queued --worker claims--> running --last node--------------------------> succeeded
                                 |  \\--node fails / step cap / gate refused--> failed
                                 |  \\--cancel requested (between steps)-----> cancelled
                                 \\--approval node--> waiting_approval --approve--> queued (re-enqueued)
                                                          \\--reject / cancel--> cancelled

Durability rules
----------------
* A step is persisted (row + run pointer + counters) in ONE transaction, only after the node finished.
  Resume therefore derives everything from persisted rows: run.current_node_id says what runs next, and the
  step rows rebuild the template context. Nothing lives only in memory.
* The dangerous window is "side effect sent, step not yet persisted". On resume the node simply runs again
  and sends the SAME Idempotency-Key, so the mock world absorbs the duplicate (exactly-once effect).
* Approval gating and the step cap are checked here, from data, before a node runs. Model output cannot
  influence either: only the approve/reject API can create an approved Approval row.
"""
from __future__ import annotations

import time

from .catalog import Catalog
from .config import Settings
from .db import Store
from .nodes import EXECUTORS, ExecEnv, StepCancelled, StepFailure
from .providers import Provider
from .jobqueue import remove
from .templates import TemplateError, render
from .util import dumps, loads, new_id, now_iso

TERMINAL = {"succeeded", "failed", "cancelled"}


class LeaseLost(Exception):
    """Another worker owns this job now (our lease expired); stop without touching state."""


class SimulatedCrash(BaseException):
    """Test hook: behaves like kill -9 (not an Exception, so nothing downstream 'handles' it)."""


class Engine:
    def __init__(self, store: Store, settings: Settings, catalog: Catalog, provider: Provider,
                 fault_hook=None, sleep=time.sleep):
        self.store, self.settings, self.catalog, self.provider = store, settings, catalog, provider
        self.fault_hook = fault_hook or (lambda point, run_id, node_id: None)
        self.sleep = sleep

    # ------------------------------------------------------------------ main loop
    def process_run(self, run_id: str, worker_id: str | None = None) -> None:
        while True:
            run = self.store.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
            if run is None or run["status"] in TERMINAL or run["status"] == "waiting_approval":
                return
            if run["status"] == "queued":
                with self.store.tx() as c:
                    c.execute("UPDATE runs SET status='running' WHERE run_id=? AND status='queued'", (run_id,))
                continue
            if not self._step(run, worker_id):
                return

    def _step(self, run: dict, worker_id) -> bool:
        """Executes the run's current node. Returns True to keep looping, False when the run stopped."""
        run_id = run["run_id"]
        defn = loads(run["definition_snapshot"])
        nodes = {n["id"]: n for n in defn["nodes"]}
        node_id = run["current_node_id"]

        if run["cancel_requested"]:
            self._finish(run_id, "cancelled", "cancel_requested", None, worker_id)
            return False
        if node_id is None:
            self._finish(run_id, "succeeded", None, None, worker_id)
            return False
        node = nodes.get(node_id)
        if node is None:
            self._fail_run(run_id, "invalid_state", f"current node '{node_id}' is not in the run snapshot", node_id, worker_id)
            return False

        max_steps = (defn.get("limits") or {}).get("max_steps") or self.settings.default_max_steps
        if run["steps_executed"] >= max_steps:  # checked BEFORE every node execution, loops included
            self._fail_run(run_id, "max_steps_exceeded",
                           f"Run exceeded limits.max_steps ({max_steps}) before executing node '{node_id}'",
                           node_id, worker_id)
            return False

        seq = run["steps_executed"] + 1
        steps = self.store.all("SELECT * FROM steps WHERE run_id=? ORDER BY sequence", (run_id,))
        ctx = self._context(run, steps)
        ntype = node["type"]
        started_at, t0 = now_iso(), time.monotonic()

        if self.catalog.requires_approval(ntype) and not self._has_approved(run_id):
            self._fail_step(run, node, seq, started_at, t0, worker_id,
                            StepFailure("approval_required",
                                        f"Node '{node_id}' ({ntype}) requires an approved approval earlier in this run"))
            return False
        if ntype == "approval":
            return self._enter_approval(run, node, seq, ctx, started_at, t0, worker_id)

        visit = sum(1 for s in steps if s["node_id"] == node_id)
        key = f"{run_id}:{node_id}" + (f":{visit + 1}" if visit else "")  # stable across retries AND resumes
        env = ExecEnv(self.settings, self.provider, key,
                      interrupted=lambda: self._cancel_requested(run_id),
                      begin_delay=lambda secs: self._begin_delay(run_id, secs), sleep=self.sleep)
        try:
            result = EXECUTORS[ntype](node, ctx, env)
        except StepFailure as failure:
            self._fail_step(run, node, seq, started_at, t0, worker_id, failure)
            return False
        except StepCancelled:
            self._record_step(run, node, seq, "cancelled", started_at, t0, worker_id)
            self._finish(run_id, "cancelled", "cancel_requested", None, worker_id)
            return False

        self.fault_hook("after_effect", run_id, node_id)  # crash window: effect sent, step not persisted
        next_id = (node.get("on_true") if result.branch else node.get("on_false")) if ntype == "condition" else node.get("next")
        self._record_step(run, node, seq, "succeeded", started_at, t0, worker_id, result=result, next_id=next_id)
        return True

    # ------------------------------------------------------------------ approval pause
    def _enter_approval(self, run, node, seq, ctx, started_at, t0, worker_id) -> bool:
        try:
            message = render(node["params"]["message"], ctx)
        except TemplateError as e:
            self._fail_step(run, node, seq, started_at, t0, worker_id, StepFailure("template_error", str(e)))
            return False
        approval_id = new_id("apr")
        with self.store.tx() as c:
            self._assert_lease(c, run["run_id"], worker_id)
            c.execute("INSERT INTO approvals(id, run_id, node_id, message, status, created_at) VALUES (?,?,?,?, 'pending', ?)",
                      (approval_id, run["run_id"], node["id"], message, now_iso()))
            self._insert_step(c, run["run_id"], seq, node, "waiting", started_at, t0,
                              resolved_input={"message": message}, approval_id=approval_id)
            c.execute("UPDATE runs SET status='waiting_approval', steps_executed=steps_executed+1 WHERE run_id=?",
                      (run["run_id"],))
            remove(c, run["run_id"])  # a paused run holds no worker; approving re-enqueues it
        return False

    # ------------------------------------------------------------------ persistence helpers
    def _insert_step(self, c, run_id, seq, node, status, started_at, t0, resolved_input=None, output=None,
                     tokens=None, key=None, approval_id=None, error=None, attempt=1):
        c.execute(
            "INSERT INTO steps(run_id, sequence, node_id, node_type, status, attempt, resolved_input, output, "
            "tokens_prompt, tokens_completion, idempotency_key, approval_id, error, started_at, duration_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, seq, node["id"], node["type"], status, attempt, dumps(resolved_input), dumps(output),
             tokens[0] if tokens else None, tokens[1] if tokens else None, key, approval_id, dumps(error),
             started_at, int((time.monotonic() - t0) * 1000)))

    def _record_step(self, run, node, seq, status, started_at, t0, worker_id, result=None, next_id=None):
        """Persist-then-advance: the step row and the run's next pointer commit atomically."""
        run_id = run["run_id"]
        tokens = result.tokens if result else None
        with self.store.tx() as c:
            self._assert_lease(c, run_id, worker_id)
            self._insert_step(c, run_id, seq, node, status, started_at, t0,
                              resolved_input=result.resolved_input if result else None,
                              output=result.output if result else None, tokens=tokens,
                              key=result.idempotency_key if result else None, attempt=result.attempts if result else 1)
            spent = (tokens[0] + tokens[1]) if tokens else 0
            if status == "succeeded" and next_id is None:
                c.execute("UPDATE runs SET status='succeeded', current_node_id=NULL, steps_executed=steps_executed+1, "
                          "ai_tokens_used=ai_tokens_used+?, wake_at=NULL, finished_at=? WHERE run_id=?",
                          (spent, now_iso(), run_id))
                remove(c, run_id)
            elif status == "succeeded":
                c.execute("UPDATE runs SET current_node_id=?, steps_executed=steps_executed+1, "
                          "ai_tokens_used=ai_tokens_used+?, wake_at=NULL WHERE run_id=?", (next_id, spent, run_id))
            else:
                c.execute("UPDATE runs SET steps_executed=steps_executed+1 WHERE run_id=?", (run_id,))

    def _fail_step(self, run, node, seq, started_at, t0, worker_id, failure: StepFailure):
        error = {"code": failure.code, "message": failure.message, "node_id": node["id"]}
        with self.store.tx() as c:
            self._assert_lease(c, run["run_id"], worker_id)
            self._insert_step(c, run["run_id"], seq, node, "failed", started_at, t0,
                              resolved_input=failure.resolved_input, tokens=failure.tokens,
                              error=error, attempt=failure.attempts)
            spent = sum(failure.tokens) if failure.tokens else 0
            c.execute("UPDATE runs SET status='failed', error=?, steps_executed=steps_executed+1, "
                      "ai_tokens_used=ai_tokens_used+?, finished_at=? WHERE run_id=?",
                      (dumps(error), spent, now_iso(), run["run_id"]))
            remove(c, run["run_id"])

    def _fail_run(self, run_id, code, message, node_id, worker_id):
        self._finish(run_id, "failed", None, {"code": code, "message": message, "node_id": node_id}, worker_id)

    def _finish(self, run_id, status, ended_by, error, worker_id):
        with self.store.tx() as c:
            self._assert_lease(c, run_id, worker_id)
            c.execute("UPDATE runs SET status=?, ended_by=?, error=?, current_node_id=CASE WHEN ?='succeeded' THEN NULL "
                      "ELSE current_node_id END, wake_at=NULL, finished_at=? WHERE run_id=? AND status NOT IN "
                      "('succeeded','failed','cancelled')", (status, ended_by, dumps(error), status, now_iso(), run_id))
            remove(c, run_id)

    def fail_internal(self, run_id: str, exc: Exception) -> None:
        """Unexpected engine bug: fail the run visibly instead of redelivering it forever."""
        self._finish(run_id, "failed", None, {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}",
                                              "node_id": None}, None)

    def _assert_lease(self, c, run_id, worker_id):
        if worker_id is None:
            return
        job = c.execute("SELECT worker_id FROM queue_jobs WHERE run_id=?", (run_id,)).fetchone()
        if job is None or job["worker_id"] != worker_id:
            raise LeaseLost(run_id)

    # ------------------------------------------------------------------ small queries
    @staticmethod
    def _context(run: dict, steps: list[dict]) -> dict:
        nodes = {s["node_id"]: {"output": loads(s["output"], {})} for s in steps if s["status"] == "succeeded"}
        return {"trigger": {"body": loads(run["input"], {})}, "nodes": nodes}

    def _has_approved(self, run_id: str) -> bool:
        return self.store.one("SELECT 1 AS ok FROM approvals WHERE run_id=? AND status='approved' LIMIT 1", (run_id,)) is not None

    def _cancel_requested(self, run_id: str) -> bool:
        row = self.store.one("SELECT cancel_requested FROM runs WHERE run_id=?", (run_id,))
        return bool(row and row["cancel_requested"])

    def _begin_delay(self, run_id: str, seconds: float) -> float:
        with self.store.tx() as c:
            row = c.execute("SELECT wake_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row["wake_at"]:
                return row["wake_at"]  # resuming after a crash: keep the original deadline
            wake_at = time.time() + seconds
            c.execute("UPDATE runs SET wake_at=? WHERE run_id=?", (wake_at, run_id))
            return wake_at
