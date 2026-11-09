import json
import unittest

from relay.errors import RelayError
from tests.helpers import RelayTestCase, notify, workflow

GATED = workflow("gated", [
    {"id": "gate", "type": "approval", "params": {"message": "ok?"}, "next": "after"}, notify("after")])


class Lifecycle(RelayTestCase):
    def status_of(self, fn, *a):
        try:
            fn(*a)
        except RelayError as e:
            return e.status, e.code
        return None

    def test_draft_publish_lifecycle_and_frozen_published_workflows(self):
        wf = workflow("w1", [notify("n1")])
        self.assertEqual(self.relay.create_workflow(wf)["status"], "draft")
        self.assertEqual(self.status_of(self.relay.trigger, "w1", {}), (409, "workflow_not_published"))
        self.assertEqual(self.relay.publish_workflow("w1")["status"], "published")
        self.assertEqual(self.status_of(self.relay.update_workflow, "w1", wf), (409, "workflow_published"))
        self.assertEqual(self.status_of(self.relay.create_workflow, wf), (409, "workflow_exists"))

    def test_invalid_draft_can_be_saved_but_not_published(self):
        bad = workflow("w2", [{"id": "n1", "type": "teleport", "params": {}}])
        self.relay.create_workflow(bad)
        self.assertEqual(self.status_of(self.relay.publish_workflow, "w2"), (422, "invalid_node_type"))
        self.assertEqual(self.relay.get_workflow("w2")["status"], "draft")

    def test_run_executes_its_snapshot_not_the_live_workflow(self):
        wid = self.deploy(workflow("w3", [notify("n1", message="original")]))
        run_id = self.relay.trigger(wid, {})["run_id"]
        with self.relay.store.tx() as c:  # someone tampers with the live definition afterwards
            c.execute("UPDATE workflows SET definition=replace(definition, 'original', 'CHANGED') WHERE id='w3'")
        self.drain()
        self.assertEqual(self.ledger()[0]["payload"]["message"], "original")
        self.assertIn("original", self.relay.get_run(run_id)["steps"][0]["input"]["message"])

    def test_webhook_secret_is_enforced(self):
        wid = self.deploy(workflow("hook", [notify("n1")], trigger={"type": "webhook", "secret": "s3cret"}))
        self.assertEqual(self.status_of(self.relay.webhook, wid, None, {}), (401, "invalid_webhook_secret"))
        self.assertEqual(self.status_of(self.relay.webhook, wid, "wrong", {}), (401, "invalid_webhook_secret"))
        run_id = self.relay.webhook(wid, "s3cret", {"a": 1})["run_id"]
        self.assertEqual(self.relay.get_run(run_id)["input"], {"a": 1})
        self.assertEqual(self.relay.get_run(run_id)["trigger_type"], "webhook")

    def test_webhook_secret_is_never_returned_by_the_api(self):
        self.deploy(workflow("hook2", [notify("n1")], trigger={"type": "webhook", "secret": "s3cret-value"}))
        self.assertNotIn("s3cret-value", json.dumps(self.relay.get_workflow("hook2")))
        self.assertNotIn("s3cret-value", json.dumps(self.relay.list_workflows()))
        run_id = self.relay.webhook("hook2", "s3cret-value", {})["run_id"]
        self.drain()
        self.assertNotIn("s3cret-value", json.dumps(self.relay.get_run(run_id)))

    def test_world_outage_shorter_than_the_retry_window_is_survived(self):
        self.world.fail_next = 5  # five 503s in a row, then the world "comes back"
        run = self.run_workflow(workflow("outage", [notify("n1")]))
        self.assertEqual((run["status"], run["steps"][0]["attempt"]), ("succeeded", 6))
        self.assertEqual(len(self.ledger()), 1)

    def test_approve_twice_is_a_409_and_unknown_ids_are_404(self):
        wid = self.deploy(GATED)
        self.relay.trigger(wid, {})
        self.drain()
        aid = self.relay.list_approvals("pending")[0]["id"]
        self.relay.decide(aid, True)
        self.assertEqual(self.status_of(self.relay.decide, aid, True), (409, "approval_already_decided"))
        self.assertEqual(self.status_of(self.relay.decide, "apr_nope", True), (404, "approval_not_found"))
        self.assertEqual(self.status_of(self.relay.get_run, "run_nope"), (404, "run_not_found"))

    def test_cancel_waiting_run_closes_its_approval(self):
        wid = self.deploy(GATED)
        run_id = self.relay.trigger(wid, {})["run_id"]
        self.drain()
        self.assertEqual(self.relay.cancel_run(run_id)["status"], "cancelled")
        self.assertEqual(self.relay.list_approvals("pending"), [])
        self.assertEqual(self.ledger(), [])

    def test_cancel_queued_run_executes_nothing_and_terminal_cancel_is_409(self):
        wid = self.deploy(workflow("w4", [notify("n1")]))
        run_id = self.relay.trigger(wid, {})["run_id"]
        run = self.relay.cancel_run(run_id)
        self.assertEqual((run["status"], run["ended_by"]), ("cancelled", "cancel_requested"))
        self.drain()
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.status_of(self.relay.cancel_run, run_id), (409, "run_already_finished"))

    def test_cancel_running_run_stops_between_steps(self):
        calls = []

        def cancel_after_first(point, run_id, node_id):
            if node_id == "n1" and not calls:
                calls.append(1)
                self.relay.cancel_run(run_id)  # request cancellation while n1 is "in flight"

        self.build_engine(self.provider_obj, fault_hook=cancel_after_first)
        run = self.run_workflow(workflow("w5", [notify("n1", nxt="n2"), notify("n2")]))
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual([s["node_id"] for s in run["steps"]], ["n1"], "in-flight step finished, no further nodes ran")
        self.assertEqual(len(self.ledger()), 1)

    def test_condition_routing_and_template_failure_is_a_clear_step_error(self):
        cond = {"id": "c", "type": "condition", "params": {"left": "{{trigger.body.amount}}", "op": "greater_than",
                                                           "right": "100"}, "on_true": "big", "on_false": "small"}
        wf = workflow("w6", [cond, notify("big", message="big"), notify("small", message="small")])
        self.deploy(wf)
        r1 = self.relay.trigger("w6", {"amount": 250})["run_id"]
        r2 = self.relay.trigger("w6", {"amount": 40})["run_id"]
        r3 = self.relay.trigger("w6", {})["run_id"]
        self.drain()
        self.assertEqual(self.relay.get_run(r1)["steps"][-1]["node_id"], "big")
        self.assertEqual(self.relay.get_run(r2)["steps"][-1]["node_id"], "small")
        bad = self.relay.get_run(r3)
        self.assertEqual((bad["status"], bad["error"]["code"]), ("failed", "template_error"))


if __name__ == "__main__":
    unittest.main()
