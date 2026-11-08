"""Business operations behind the API: workflows, triggers, runs, approvals, cancellation.

Pure Python, no HTTP: server.py is a thin adapter, and tests call this directly.
"""
from __future__ import annotations

import hmac
import json

from .catalog import Catalog
from .config import Settings
from .db import Store
from .errors import RelayError
from .jobqueue import enqueue, remove
from .util import dumps, loads, new_id, now_iso
from .validation import check_shape, validate_definition

TERMINAL = {"succeeded", "failed", "cancelled"}


class Relay:
    def __init__(self, settings: Settings, store: Store | None = None):
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.catalog = Catalog.load(settings.data_dir / "node_catalog.json")

    # ------------------------------------------------------------------ workflows
    def create_workflow(self, defn: dict) -> dict:
        check_shape(defn)
        if self.store.one("SELECT 1 AS x FROM workflows WHERE id=?", (defn["id"],)):
            raise RelayError(409, "workflow_exists", f"Workflow '{defn['id']}' already exists")
        now = now_iso()
        with self.store.tx() as c:
            c.execute("INSERT INTO workflows(id, name, description, status, definition, created_at, updated_at) "
                      "VALUES (?,?,?, 'draft', ?, ?, ?)",
                      (defn["id"], defn["name"], defn.get("description", ""), dumps(defn), now, now))
        return self.get_workflow(defn["id"])

    def update_workflow(self, workflow_id: str, defn: dict) -> dict:
        wf = self._workflow(workflow_id)
        if wf["status"] == "published":
            raise RelayError(409, "workflow_published", "Published workflows are frozen; create a new workflow id to change it")
        check_shape(defn)
        if defn["id"] != workflow_id:
            raise RelayError(422, "invalid_definition", "Definition id must match the URL")
        with self.store.tx() as c:
            c.execute("UPDATE workflows SET name=?, description=?, definition=?, updated_at=? WHERE id=?",
                      (defn["name"], defn.get("description", ""), dumps(defn), now_iso(), workflow_id))
        return self.get_workflow(workflow_id)

    def publish_workflow(self, workflow_id: str) -> dict:
        wf = self._workflow(workflow_id)
        validate_definition(loads(wf["definition"]), self.catalog)
        with self.store.tx() as c:
            c.execute("UPDATE workflows SET status='published', updated_at=? WHERE id=?", (now_iso(), workflow_id))
        return self.get_workflow(workflow_id)

    def list_workflows(self) -> list[dict]:
        rows = self.store.all("SELECT * FROM workflows ORDER BY created_at, id")
        return [self._present_workflow(r, with_definition=False) for r in rows]

    def get_workflow(self, workflow_id: str) -> dict:
        return self._present_workflow(self._workflow(workflow_id), with_definition=True)

    def seed(self) -> list[str]:
        """Load data/seed_workflows.json as Published workflows (skips ones that already exist)."""
        path = self.settings.data_dir / "seed_workflows.json"
        loaded = []
        for defn in json.loads(path.read_text(encoding="utf-8"))["workflows"]:
            if self.store.one("SELECT 1 AS x FROM workflows WHERE id=?", (defn["id"],)):
                continue
            self.create_workflow(defn)
            self.publish_workflow(defn["id"])
            loaded.append(defn["id"])
        return loaded

    # ------------------------------------------------------------------ triggers
    def trigger(self, workflow_id: str, body, trigger_type: str = "manual") -> dict:
        wf = self._workflow(workflow_id)
        if wf["status"] != "published":
            raise RelayError(409, "workflow_not_published", f"Workflow '{workflow_id}' is a draft; publish it first")
        defn = loads(wf["definition"])
        run_id = new_id("run")
        with self.store.tx() as c:  # run row + queue job commit together: no run without a job
            c.execute("INSERT INTO runs(run_id, workflow_id, definition_snapshot, status, trigger_type, input, "
                      "current_node_id, started_at) VALUES (?,?,?, 'queued', ?, ?, ?, ?)",
                      (run_id, workflow_id, dumps(defn), trigger_type, dumps(body if body is not None else {}),
                       defn["entry"], now_iso()))
            enqueue(c, run_id)
        return {"run_id": run_id, "id": run_id, "status": "queued"}

    def webhook(self, workflow_id: str, provided_secret: str | None, body) -> dict:
        wf = self._workflow(workflow_id)
        trigger = loads(wf["definition"]).get("trigger", {})
        if trigger.get("type") != "webhook":
            raise RelayError(400, "not_a_webhook_workflow", f"Workflow '{workflow_id}' does not use a webhook trigger")
        expected = trigger.get("secret", "")
        if not provided_secret or not hmac.compare_digest(provided_secret.encode(), expected.encode()):
            raise RelayError(401, "invalid_webhook_secret", "Missing or invalid X-Relay-Secret")
        return self.trigger(workflow_id, body, "webhook")

    # ------------------------------------------------------------------ runs
    def get_run(self, run_id: str) -> dict:
        run = self.store.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if run is None:
            raise RelayError(404, "run_not_found", f"Run '{run_id}' not found")
        steps = self.store.all("SELECT * FROM steps WHERE run_id=? ORDER BY sequence", (run_id,))
        return self._present_run(run, steps)

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self.store.all("SELECT * FROM runs ORDER BY started_at DESC, run_id LIMIT ?", (limit,))
        return [self._present_run(r, None) for r in rows]

    def cancel_run(self, run_id: str) -> dict:
        with self.store.tx() as c:
            run = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise RelayError(404, "run_not_found", f"Run '{run_id}' not found")
            status = run["status"]
            if status in TERMINAL:
                raise RelayError(409, "run_already_finished", f"Run is already {status}")
            if status == "running":  # cooperative: engine checks between steps; in-flight step may finish
                c.execute("UPDATE runs SET cancel_requested=1 WHERE run_id=?", (run_id,))
            else:  # queued or waiting_approval: nothing is executing, cancel immediately
                c.execute("UPDATE runs SET status='cancelled', ended_by='cancel_requested', cancel_requested=1, "
                          "finished_at=? WHERE run_id=?", (now_iso(), run_id))
                c.execute("UPDATE approvals SET status='cancelled', decided_by=?, decided_at=? "
                          "WHERE run_id=? AND status='pending'", (self.settings.decider, now_iso(), run_id))
                c.execute("UPDATE steps SET status='cancelled' WHERE run_id=? AND status='waiting'", (run_id,))
                remove(c, run_id)
        return self.get_run(run_id)

    # ------------------------------------------------------------------ approvals
    def list_approvals(self, status: str | None = None) -> list[dict]:
        rows = (self.store.all("SELECT * FROM approvals WHERE status=? ORDER BY created_at", (status,)) if status
                else self.store.all("SELECT * FROM approvals ORDER BY created_at DESC"))
        return rows

    def decide(self, approval_id: str, approve: bool, decided_by: str | None = None) -> dict:
        who = decided_by or self.settings.decider
        with self.store.tx() as c:
            appr = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if appr is None:
                raise RelayError(404, "approval_not_found", f"Approval '{approval_id}' not found")
            if appr["status"] != "pending":
                raise RelayError(409, "approval_already_decided", f"Approval is already {appr['status']}")
            run = c.execute("SELECT * FROM runs WHERE run_id=?", (appr["run_id"],)).fetchone()
            now = now_iso()
            c.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                      ("approved" if approve else "rejected", who, now, approval_id))
            if approve:
                defn = loads(run["definition_snapshot"])
                node = next(n for n in defn["nodes"] if n["id"] == appr["node_id"])
                c.execute("UPDATE steps SET status='succeeded', output=? WHERE approval_id=?",
                          (dumps({"decision": "approved", "decided_by": who}), approval_id))
                c.execute("UPDATE runs SET status='queued', current_node_id=? WHERE run_id=?",
                          (node.get("next"), run["run_id"]))
                enqueue(c, run["run_id"])  # same transaction: decision and re-enqueue are atomic
            else:
                c.execute("UPDATE steps SET status='rejected', output=? WHERE approval_id=?",
                          (dumps({"decision": "rejected", "decided_by": who}), approval_id))
                c.execute("UPDATE runs SET status='cancelled', ended_by='approval_rejected', error=?, finished_at=? "
                          "WHERE run_id=?", (dumps({"code": "approval_rejected", "node_id": appr["node_id"],
                                                    "message": f"Rejected by {who}"}), now, run["run_id"]))
        return self.store.one("SELECT * FROM approvals WHERE id=?", (approval_id,))

    # ------------------------------------------------------------------ internals
    def _workflow(self, workflow_id: str) -> dict:
        wf = self.store.one("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if wf is None:
            raise RelayError(404, "workflow_not_found", f"Workflow '{workflow_id}' not found")
        return wf

    @staticmethod
    def _present_workflow(row: dict, with_definition: bool) -> dict:
        out = {"id": row["id"], "name": row["name"], "description": row["description"], "status": row["status"],
               "created_at": row["created_at"], "updated_at": row["updated_at"]}
        defn = loads(row["definition"])
        out["trigger_type"] = (defn.get("trigger") or {}).get("type")
        if with_definition:
            out["definition"] = defn
        return out

    @staticmethod
    def _present_run(run: dict, steps: list[dict] | None) -> dict:
        out = {"run_id": run["run_id"], "workflow_id": run["workflow_id"], "status": run["status"],
               "trigger_type": run["trigger_type"], "input": loads(run["input"]),
               "current_node_id": run["current_node_id"], "steps_executed": run["steps_executed"],
               "ai_tokens_used": run["ai_tokens_used"], "error": loads(run["error"]), "ended_by": run["ended_by"],
               "cancel_requested": bool(run["cancel_requested"]),
               "started_at": run["started_at"], "finished_at": run["finished_at"]}
        if steps is not None:
            out["steps"] = [{
                "node_id": s["node_id"], "type": s["node_type"], "sequence": s["sequence"], "status": s["status"],
                "attempt": s["attempt"], "input": loads(s["resolved_input"]), "output": loads(s["output"]),
                "tokens": ({"prompt": s["tokens_prompt"], "completion": s["tokens_completion"]}
                           if s["tokens_prompt"] is not None else None),
                "idempotency_key": s["idempotency_key"], "approval_id": s["approval_id"],
                "error": loads(s["error"]), "started_at": s["started_at"], "duration_ms": s["duration_ms"],
            } for s in steps]
        return out
